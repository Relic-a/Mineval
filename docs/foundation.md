# Preparation architecture

`preparation.Store` starts at an external candidate. Git blobs or raw tree/archive
sources become a mutable draft and private source evidence. `verification.diagnose`
checks only a consistent draft copy. `service` exposes this operation to a running
cleanup process over a task-scoped Unix socket. `sandbox` owns rootless Docker
lifecycle and does not know which agent or benchmark harness invokes commands.
`artifacts` owns hashes, safe exports, archive handling, and filesystem locks.

`intake` normalizes portable selector records, assigns missing task IDs and resolves
CLI source paths relative to the handoff JSON. Metadata includes an explicit intake
completeness report; legacy notes are not invented. Deferred/missing-boundary records
store only metadata, state and a metadata receipt, with `needs-review` state and no
draft. Rejected selector records likewise create no draft. Operational entry points
block those records, while task list/show remain available. See the
[portable skill](../skills/mineval-moment-selector/SKILL.md) for the producer contract.

```text
external candidate -> import -> draft/workspace -> review + freeze -> version/snapshot.tar.gz
                       |              |                                     |
                       v              v                                     v
                 private evidence   fresh diagnostic sandbox        new per-run working copies
                       |
                       v
                 private extraction -> optional isolated validation -> independent review
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
    runs/<run-id>.json             immutable reserved execution identity (API only)
    runs/<run-id>.sha256            identity integrity receipt
    evaluations/<evaluation-id>.json  immutable reserved evaluation identity (API only)
    evaluations/<evaluation-id>.sha256  identity integrity receipt
    diagnostics/<id>.json          diagnostic report, with separate hash receipt
    verify-latest.json             latest-report convenience copy
    cleanup-latest.json            latest cleanup producer result, never approval
    cleanups/<cleanup-id>/         append-only attempt artifacts
      workspace.tar.gz             tree only, no agent-controlled Git
      tree-hashes.json
      cleanup-changes.patch         all differences from original base
      verify-config.json
      cleanup-report.json          reasons, prerequisites, producer, diagnostic IDs
      artifact-hashes.json
    rejections/<id>.json           explicit rejection reason, with hash receipt
    versions/<version>/
      snapshot.tar.gz
      workspace-format.json        explicit tree/history format (absent in legacy revisions)
      snapshot.sha256
      tree-hashes.json
      cleanup-changes.patch
      cleanup-report.json
      cleanup-attempt.json         matching ready-for-review cleanup attempt
      verify-config.json
      last-diagnostic.json
      approval.json                exact independent review receipt
      private/{verification,review,validations}/  never exported to benchmark workspaces
      artifact-hashes.json
    private/
      base/                        original base for review/diff, never agent-mounted
      future/<commit>/             imported future source/evidence
      diffs/<commit>.patch          Git imports only
      extraction-selection.json    optional extraction-session suggestion
      extraction-attempts/<session>/  unavailable verification reports
      reviews/<session>/           verdict/reasons, exact input hashes and producer
      validations/<extraction>/<id>/  private fresh-container diagnostics
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
`Store.reserve_run(task_id, run_id, version, model=..., harness=...)` reserves a
task-local ID with schema version 1, status `reserved`, an exact prepared-revision
reference (including both hashes), and separate model/harness identities. Each identity
requires `name`, `version` and a JSON-object `configuration`; this is the comparison
identity, with no ranking policy. `Store.reserve_evaluation(task_id, evaluation_id,
run_id)` reserves a task-local evaluation ID, pins the run ID and run JSON hash, and
also reports `reserved`. Both records have read-only files and hash receipts; duplicate
IDs fail. These minimal identity reservations extend the original namespace-only
foundation. They do not claim execution, completion, failure, or a score. Later phases
must attach execution/results without replacing identity records. No reservation CLI
is provided: `bench run` and `evaluate` still fail without creating records.

`task show` lists record filenames; `task run TASK RUN` and `task evaluation TASK EVAL`
inspect their receipts and JSON. `suite list` lists validated suite definitions. Retired
tasks, revisions and records remain inspectable; retirement still blocks new preparation,
suite enrollment, reservations and workspace creation.

New revisions declare `workspace-format.json` schema version 1 with
`tree: {"format": "tar", "path": "snapshot.tar.gz"}`. Phase 3 Git imports save a
hashed `private/history.pack` containing precisely the base-reachable object closure.
New revisions publish `git_history: {"format": "base-pack-v1", "path": "history.pack",
"base_commit": "<full SHA>"}`. Materialization verifies the receipt, exports the
reviewed tree and reconstructs .git with only historical-base at the base commit.
Preparation edits remain uncommitted; source Git configuration is never copied.

Missing descriptors or `git_history: {"format": "none"}` retain legacy tree-only
behavior. Raw exports cannot manufacture history; existing revisions are never
rewritten. Reimport complete Git sources and publish new revisions to gain history.
The dedicated agent runner refuses tree-only inputs. Unknown formats fail before
creating destinations. Ordinary tree artifacts still prohibit .git; explicit trusted
history copies use bounded link-free Git-aware inventories. Never mount the source
repo. See README for Phase 3 adapter usage and limitations.

Exact layout is an implementation detail after registration, not a requirement on
an external producer. The host store is mode 0700. Cleanup receives disposable copies
of its draft and controls, reconstructed trusted historical Git, read-only instructions
and a short-lived broker directory. Extraction receives a separate private
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

Phase 4 `service.cleanup_session` runs under that lock, with dedicated adapter or
explicit argv, bridge networking and disabled agent logs. `cleanup.INSTRUCTIONS`
defines task-preserving edits and the separate producer report; it is mounted at
`/mineval-input/prompt.txt` and delivered as Codex stdin. `cleanup.save_attempt`
compares every output path to the original base, validates per-path reasons and
environment prerequisites, stores an immutable-by-API attempt and publishes the
tree/config back to the mutable draft. Each attempt has an independent ID, input
hashes, prompt hash and producer identity. No agent Git is read by host Git: tree
copying ignores its top-level `.git`, and diagnostics/materialization rebuild history
from the protected hashed pack. Legacy raw exports cannot provide historical Git.

The broker is bound to the active disposable workspace/config, never caller-supplied
paths. Fresh checks save command-level interpretations: environment failures stop
later checks, whereas behavioral test failures do not invalidate a historical base.
An optional tests-only `expected_failure` reason is a producer assertion for review.
Each attempt references its diagnostic IDs; only an exact current tree/config with
a completed/checks-failed diagnostic can be ready for review. A malformed report or
process failure is needs-review; producer reject/needs-review choices remain visible
without deleting the task or preventing a subsequent cleanup attempt. Unsupported
filesystem output is not imported. Frozen revisions copy a matching ready-for-review
attempt report and require a separate extraction session plus independent approval.

The Phase 3 dedicated adapters now also serve cleanup. There is no task discovery,
PR resolution/mining, scheduler, evaluated run coordinator, adaptive verifier or
scorer. Benchmark execution and grading remain later phases.


Phase 5 `review.session` runs extraction and independent review in new containers,
using explicit executables or the dedicated ephemeral Codex adapter. Private copies
contain base, cleaned tree, future evidence, diffs and context; reviewer copies also
contain cleanup, diagnostics, extraction and optional validation evidence. Only
structured reports survive. Agent edits cannot change the preserved source bytes.
Session IDs are host assigned; each role runs in a separate container with no reused
agent state. This enforces session separation, not different underlying model weights
or a guarantee that an external executable is intellectually independent.

Extraction manifests record focused/fuller plans, behavioral rationale, dependencies,
run instructions, limitations, source hashes and producer identity. Unavailable plans
are saved under private/extraction-attempts and cannot freeze. Legacy manual extractions
remain readable but lack the session identity required for new approval.

`review.validate` overlays original extracted files in disposable base/future copies
and calls diagnostics without updating the cleanup's latest report. Append-only
private/validations/<extraction>/<id> receipts retain inputs, configuration, image and
command outcomes. Environment failures are distinct from behavioral failures; neither
base failure nor future success is automatic approval. Validation is optional where
infeasible. Future checks use original future trees without cleanup adaptations.

Private review reports bind exact cleanup/extraction manifests, current diagnostic,
metadata and validation receipts. Approve/revise/reject and reasons are mandatory.
New `finalize --review-id` requires approval and copies private verification, review
and validations into the immutable revision. `snapshot.tar.gz` and materialized
workspaces contain only the cleaned tree plus separately reconstructed pre-base Git;
private revision subdirectories must never be mounted for benchmark agents. Existing
frozen versions remain readable, while inline legacy review attestations cannot create
new revisions. The owning OS user remains outside this application integrity boundary.
