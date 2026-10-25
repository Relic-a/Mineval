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
    runs/<run-id>.json             immutable launch identity (legacy reservations readable)
    runs/<run-id>.sha256            identity integrity receipt
    run-outputs/<run-id>/          separately sealed benchmark result
      terminal.json               status + identity hash; no task verdict
      agent/result.json           small structured completion and operational receipt
      agent/workspace.tar.gz      final tree, no agent Git or transcripts
      artifact-hashes.json        covers all result bytes
    evaluations/<evaluation-id>.json  immutable assessment (legacy reservations readable)
    evaluations/<evaluation-id>.sha256  assessment integrity receipt
    evaluation-evidence/<id>/      sealed config, original checks, outcomes, adaptations
      baseline/                   selected-check hashes + original execution
      adapted/                    optional adapted execution from fresh saved tree
      assessment.json             optional evaluator proposal; no session logs
      report.json                 verdict, explanation, limitations
      artifact-hashes.json        all evaluation evidence hashes
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
      task-instructions.json      frozen outcome statement (new revisions)
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
requires `name`, `version` and a JSON-object `configuration`; this is legacy reservation metadata,
not a harness-configuration ranking policy. `Store.reserve_evaluation(task_id, evaluation_id,
run_id)` reserves a task-local evaluation ID, pins the run ID and run JSON hash, and
also reports `reserved`. Both records have read-only files and hash receipts; duplicate
IDs fail. These minimal identity reservations extend the original namespace-only
foundation. They do not claim execution, completion, failure, or a score. Later phases
must attach execution/results without replacing identity records. No reservation CLI
is provided. `bench run` now writes schema-2 launch identities and appends sealed
`run-outputs` without replacing those identities. Phase 7 `evaluate` writes separate
schema-2 assessments tied to the sealed outputs.
`Store.inspect_run` validates identity/output receipts and joins them for inspection;
`reserve_evaluation` still reserves a legacy reference only, with no grading claim.

Phase 6 orchestration lives in `benchmark.py`: `run`, `run_suite`, `list_runs`,
`export`, `compare` and `recover`. New attempts copy the selected statement, full
instruction hash, exact revision, optional suite hash, model + harness name and
operational environment. Frozen diagnostic image digests are used when available;
resolved image ID is retained. Models/harnesses are comparison identities; timeout
and other operational settings are recorded without creating ranking dimensions.
The task lock spans launch and result collection. Each agent receives only a second
disposable copy of materialized history, inputs and completion control. Publication
renames a staged, hashed output directory atomically; terminal records never change.
An interrupted host may leave an immutable `running` identity; explicit `recover`
seals an infrastructure error under the same lock. Normal Ctrl-C/SIGTERM collects
safe partial output after container removal. SIGKILL cannot guarantee cleanup or
workspace retention. No raw exceptions/subprocess logs/transcripts are retained.
Comparison takes explicit attempt selections, reports exact common revisions and
coverage, and provides no evaluation scores. See README for CLI/exit-code details.

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

The Phase 3 dedicated adapters also serve cleanup, private review, benchmarks and
evaluators. There is no task discovery pipeline, PR mining or scheduler.


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


Phase 7 orchestration lives in `evaluation.py`: `evaluate`, `inspect`, `compare`.
The task lock covers input validation, baseline execution, optional separate evaluator
and one adapted-check execution, plus append-only evidence/record publication. Every
execution receives a fresh saved-tree copy. Original frozen checks replace candidate
checks and mount as individual read-only nested binds through
`SandboxSpec.readonly_paths`. Only declared private paths may be adapted, with full
replacement content, source commit/path references and justification. Evaluator
production edits are discarded. Its inputs include the frozen task statement,
extraction plan and original bytes, config and baseline command results. A fresh
ephemeral Codex adapter or explicit isolated argv writes the structured assessment;
no evaluator logs/sessions/transcripts persist. Provenance and report shape are
validated, while semantic equivalence remains a judgment requiring inspection.

Evaluation configurations extend diagnostics with tests-only `failure_exit_codes`
(default [1]) and `inconclusive_exit_codes` (default [2]); 0 passes, all undeclared
nonzero exits and failed setup/build/smoke commands are errors. Test wrappers must
separate failed assertions from dependency/collection/execution errors; Mineval
cannot infer an arbitrary command's semantics. An evaluator may flag insufficient
or flaky evidence as inconclusive and identify execution errors explicitly. Missing
saved workspaces are inconclusive. Infrastructure failures cannot become pass/fail.
An ordinary test execution error (exit 1–124, without timeout or OOM) may still
receive one justified interface adaptation, with the verdict determined by fresh
execution. Startup errors, failed setup/build/smoke steps, missing executables,
timeout and OOM do not permit an adapted execution. The baseline evidence is retained.

Assessments pin launch identity, sealed output manifest, exact frozen revision and
verification manifest, configuration, policy and evidence hash; they get unique IDs
and incrementing per-run evaluation versions. Completed records and evidence are
immutable by API/file modes and rechecked on inspection. Legacy identity-only
reservations remain readable but cannot enter scores. Evidence seals before record
publication; host loss can leave an orphan evidence ID, never an overwritten verdict.
No in-progress evaluator session is retained or resumed; retry with a new ID.

Explicit comparison selects one assessment per task per side and one model + harness
on each side, reusing benchmark exact common-revision coverage. Common entries need
the same config, policy, verification and (when resolved on both sides) runtime image.
Only that common set contributes rates; pass/fail/inconclusive/error counts and decided
coverage remain visible. No latest/best assessment is chosen implicitly. Zero common
coverage has null rates. Re-evaluation is allowed for retired/rejected tasks because
it consumes existing saved results, without launching another benchmark.


## Phase 8 observation and retirement

`mineval observe [--port PORT]` serves `observation.py` using the Python standard
library. It binds only IPv4 loopback and prints a random per-launch URL prefix.
Host checks, no-store responses, a restrictive CSP, escaped artifact content,
no external assets and read-only GET routes keep the human view local. Do not
forward it to agents: private extraction, review and future evidence are visible.
There are no execution, retirement, session, terminal or transcript endpoints.

Task/revision/suite/run/evaluation pages use existing receipt-checked inspection
APIs. Artifact browsing is limited to named, validated preparation/result namespaces;
archives stay binary and text previews/diffs cap at 256 KiB. Diffs safely unpack
saved trees in disposable host directories and never execute candidate code or Git.
Diagnostics verify their receipts; comparison forms delegate explicit selections
to the existing exact-common-revision comparison functions. Busy tasks and corrupt
records produce an inspection error; refresh after independent CLI operations.

`task retire TASK --reason REASON` appends a retirement record and SHA receipt under
`tasks/TASK/retirements/`, then updates mutable status with the prior state. It
blocks new work through the same operation eligibility gate as rejected/deferred
tasks. Suite runs preflight every member's eligibility before any launches. Existing
suites/results are never rewritten; inspection, recovery, export and re-evaluation
continue to allow retired tasks. There is no restore/delete UI or automatic rerun.

`python -m examples.phase8_demo --directory NEW_DIRECTORY --context CONTEXT --image IMAGE`
provides a persistent real-Git/rootless/fake-agent walkthrough and summary. Private
extraction and reviewer sessions use separate scripted fixture executables, not
language models. The deliberately unsolved fake run must evaluate as fail. This
checks integration rather than the semantic quality of independent agent judgments.
The agreed pre-base Git policy in `docs/mineval-decisions.md` remains authoritative;
legacy tree-only import/archive descriptions do not prohibit sanitized Git attached
to materialized historical workspaces.
