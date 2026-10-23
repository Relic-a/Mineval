# Preparation architecture

`preparation.Store` starts at an external candidate. Git blobs or raw tree/archive
sources become a mutable draft and private source evidence. `verification.diagnose`
checks only a consistent draft copy. `service` exposes this operation to a running
cleanup process over a task-scoped Unix socket. `sandbox` owns rootless Docker
lifecycle and does not know which agent or benchmark harness invokes commands.
`artifacts` owns hashes, safe exports, archive handling, and filesystem locks.

```text
external candidate -> import -> draft/workspace -> review + freeze -> version/snapshot.tar.gz
                       |              |                                     |
                       v              v                                     v
                 private evidence   fresh diagnostic sandbox        new per-run working copies
                       |
                       v
                 private extraction (no application or execution)
```

## Stored artifacts

```text
DATA/
  locks/<task>.lock
  drafts/<task>/
    workspace/                    mutable cleaned base tree
    control/{task,verify}.json     separate, never in the frozen archive
  tasks/<task>/
    metadata.json                  original candidate + grounding classification
    base-inventory.json            baseline file hashes and executable modes
    source-hashes.json             imported private material hashes
    artifact-hashes.json           hashes of immutable registration records
    status.json                    mutable draft/frozen/rejected state
    runs/                          reserved for later benchmark records (empty)
    evaluations/                   reserved for later evaluation records (empty)
    diagnostics/<id>.json          diagnostic report, with separate hash receipt
    verify-latest.json             latest-report convenience copy
    rejections/<id>.json           explicit rejection reason, with hash receipt
    versions/<version>/
      snapshot.tar.gz
      snapshot.sha256
      tree-hashes.json
      cleanup-changes.patch
      cleanup-report.json
      verify-config.json
      last-diagnostic.json
      artifact-hashes.json
    private/
      base/                        original base for review/diff, never agent-mounted
      future/<commit>/             imported future source/evidence
      diffs/<commit>.patch          Git imports only
      extraction-selection.json    optional extraction-session suggestion
      extractions/<extraction>/
        extraction-manifest.json
        verification/<commit>/...
        artifact-hashes.json
  suites/<suite-id>/
    suite.json                     schema version, ordered pinned task/version identities and hashes
    artifact-hashes.json           integrity receipt for the immutable definition
```

Task IDs name persistent registrations; prepared revision IDs are the existing version
IDs scoped to a task. A suite pins one explicit revision per task and preserves member
order. Creating a suite validates the full prepared artifact inventory, then publishes
the definition atomically under a suite lock. Files are read-only; IDs cannot be reused.
New tasks, new versions, and later rejection do not rewrite a suite. Rejected tasks
remain inspectable but cannot be added to new suites. Existing stores without the
reserved record directories need no migration; future record writers can create them.
Run/evaluation record formats and lifecycle transitions are deliberately not defined yet.

Exact layout is an implementation detail after registration, not a requirement on
an external producer. The host store is mode 0700. Cleanup receives only its draft,
controls, and a short-lived broker directory. Extraction receives a separate private
review copy, not the cleanup draft or frozen versions. A future evaluated process
receives only a newly materialized workspace. Do not hand it the task directory.

## Isolation and lifecycle

A locally selected rootless daemon must report cgroup v2/systemd and all required
resource controllers. Images resolve to immutable local image IDs. Capabilities are
dropped, privilege escalation disabled, root filesystem read-only, init enabled,
restarts/healthchecks disabled, image-declared volumes rejected, and host networking
and arbitrary Docker flags unavailable. Images are not pulled. The API only mounts
explicit workspace/control/broker directories supplied by host orchestration.
See [Docker bind mount semantics](https://docs.docker.com/engine/storage/bind-mounts/)
and [container pause](https://docs.docker.com/reference/cli/docker/container/pause/).

Memory, CPU, PID, tmpfs and wall-clock limits are enforced. The configured
`workspace_mb` bounds the empty tmpfs for mount-free `run` operations. It does **not**
create a disk quota for bind-mounted host workspaces: deploy the data store on a
quota-limited filesystem for a hard disk budget. Imported/frozen trees have an
additional 512 MiB validation budget. In-flight agent disk writes can exceed that
budget before import/finalization; host filesystem quotas are an operational requirement
for hostile workloads. External network services are not provisioned or made deterministic.

Diagnostic stdout/stderr are drained continuously, retaining at most 64 KiB per stream
per command in memory; excess bytes are discarded. Container main-process logs use
Docker's bounded local driver. `logs_truncated` reports Python capture truncation;
Docker may have discarded earlier segments separately. Reports preserve structured
failures instead of inferring success from messages. Docker exec's reserved 126/127
codes denote command errors (125 denotes Docker control failure), so test commands should use other nonzero failure codes.

Contexts own forced container removal and anonymous-volume removal on success,
error, timeout, and keyboard interruption. Control operations have their own bounded
waits. Cleanup failure is raised or attached to the original exception. A daemon
outage, SIGKILL, or host shutdown can leave labeled `io.mineval.managed=true` containers;
inspect exact leftovers before removal. There is no global destructive cleanup.

## Deliberate boundaries and limitations

Git input requires a local repository and explicit full commit SHAs; producers acquire
remote repositories. SHA ancestry proves history boundaries, not task quality.
Raw exports require an explicit producer attestation; hashes prove retained bytes,
not their original commit identity. Optional selection notes are not inferred.
Links, submodules, non-UTF-8 Git paths and special files are rejected rather than
silently flattened. LFS pointer resolution and unavailable proprietary dependencies
belong to producer export/preparation; imported pointers are not downloaded implicitly.

The cleanup review attests that environment adaptations preserve the actual task,
and that future implementation, verification, credentials, and private artifacts do
not leak into the starting tree. Extraction's test-only/purpose fields are likewise
review assertions. These semantic properties cannot be mechanically established by
path filtering. The implementation records them and rejects missing reviews. It
never makes all-green historical tests a condition of acceptance.

A frozen version is read-only by API and file mode with integrity checks. The OS
owner can still change it deliberately; use stronger external storage retention if
needed. Binary-capable cleanup diffs are review evidence, while the exact tree
inventory/archive are authoritative reconstruction artifacts. Manifest hashes cover
their adjacent immutable artifacts; mutable status/latest-report files are not claimed
to have permanent hashes. Source trees never become evaluated-agent mounts.

Only the host service invokes Docker. The broker accepts no Docker arguments, host
paths, commands, task IDs, source evidence, or finalization requests. Commands/image/
network are read from an allowlisted configuration and run on a fresh draft copy.
The prep container is paused during copying using Docker's freezer, then resumed.
Other host writers must be stopped; inventories reject detected concurrent changes.
Task-level file locks exclude competing host operations and active sessions.

There is no model provider, prompt protocol, credential broker, task discovery,
PR resolution/mining, scheduler, evaluated run coordinator, adaptive verifier, or
scorer. The future execution adapter should materialize a separate workspace, own
a container context, deliver its chosen harness command, collect/export results,
and destroy it. Private grading remains a separate future stage.
