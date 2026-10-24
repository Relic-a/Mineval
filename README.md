# Mineval

Prepare historical software tasks supplied by an external producer. Mineval imports
selected history, isolates preparation work in rootless Docker, diagnoses drafts in
fresh containers, freezes cleaned starting trees, and privately preserves selected
future verification. Task discovery, evaluated-agent harnesses, adaptive verification,
and grading are outside this implementation.

Python 3.11+, Git, and the Docker CLI are required. The Python runtime uses only the
standard library. Install with `python -m pip install -e .`, or use
`python -m mineval` from this checkout. Docker must use a **local rootless daemon**
with cgroup v2, systemd, and delegated CPU/memory/swap/PID controllers:

```sh
mineval --context rootless doctor
```

Mineval checks these capabilities and fails closed. It never installs Docker,
changes contexts, pulls images, forwards host credentials, or mounts a Docker socket.
Images must be preloaded and contain `/bin/sh` and `sleep`; a cleanup image must also
contain Git and the `mineval` client (see [example Dockerfile](examples/Dockerfile.preparation)).
Use a trusted image without embedded credentials. Container UID 0 maps to the
unprivileged daemon owner, permitting access to the managed bind mounts.
See Docker's [rootless resource requirements](https://docs.docker.com/engine/security/rootless/tips/#limiting-resources).

## Local task registry and independent operations

```sh
mineval --data mineval-data task add candidate.json
mineval --data mineval-data task list
mineval --data mineval-data task show external-task-001
mineval --data mineval-data task revision external-task-001 VERSION
mineval --data mineval-data suite create suite.json
mineval --data mineval-data suite show baseline-v1
mineval --data mineval-data suite list
```

`task add` uses the existing import path (`import` remains supported). Task IDs are
persistent: supply one or let intake assign one from repository identity and commit
boundaries. Re-adding an ID fails. Inspection returns readable JSON.
Prepared revisions use the existing immutable `versions/<version>` artifacts created
by `finalize`; revision inspection checks their integrity receipts. `prepare` still
requires an explicit executable. `cleanup` selects a dedicated fake or Codex adapter
for the same preparation workflow; it defaults to fake.

Use [suite.json](examples/suite.json) for an explicit ordered list of task/version
pairs. Suite creation checks each prepared revision and pins its snapshot and artifact
manifest hashes. There is no implicit latest version or live pool query. A suite ID
cannot be overwritten; publish a new ID to change membership. Adding tasks or preparing
new versions leaves existing suites unchanged. Suite inspection checks the definition's
receipt; it does not revalidate the referenced task artifacts or current eligibility.

`mineval bench run` now executes frozen revisions or fixed suites with the dedicated
fake/Codex adapters. `evaluate` remains reserved for phase 7 and exits 2 without
creating records. The existing `run` command is a generic sandbox utility and
`agent-run` is the lower-level adapter primitive. Existing schema-1 identity
reservations remain readable; benchmark execution creates schema-2 identities and
separate immutable outputs. Inspect them with `bench show` or `task run TASK RUN`.

## Run a frozen benchmark

```sh
mineval --context mineval-rootless --data mineval-data bench run \
  --task external-task-001 --version VERSION --run-id attempt-001 --adapter fake
mineval --context mineval-rootless --data mineval-data bench run \
  --suite baseline-v1 --run-id baseline-attempt --adapter fake
mineval --data mineval-data bench list --task external-task-001
mineval --data mineval-data bench show external-task-001 attempt-001
mineval --data mineval-data bench export external-task-001 attempt-001 ./saved-result
mineval --data mineval-data bench compare examples/comparison.json
```

A task requires an exact `--version`; a suite uses its already pinned revisions.
Each suite member runs sequentially with its own ID (`PREFIX-001`, etc.); cancellation
stops the remaining sequence. Omit `--run-id` to allocate a new UUID. Duplicate IDs
fail; there are no automatic retries or reruns as the pool grows. The default fake
adapter produces a marker file to exercise the lifecycle; it does not solve tasks.

For an explicitly requested real run, choose `--adapter codex --model MODEL` and,
when needed, `--auth-file /path/to/model-only/auth.json`. This uses the same isolated
[adapter setup](#phase-3-isolated-agent-primitive) as `agent-run`; the frozen environment
must contain the harness. No host binary/config/home is mounted. The benchmark uses
the frozen diagnostic's image digest when available, otherwise the frozen configured
reference for older receipts, and records the actual image ID in the result. Images
must already be loaded. Frozen resource limits apply; `--timeout SECONDS` optionally
overrides the operational deadline. No token/cost budget or configuration ranking.

The agent receives a disposable materialized cleaned tree, sanitized pre-base Git,
and the outcome-oriented task statement plus solution-lookup policy. Private evidence,
future files, source URLs and reviewer artifacts are never mounted. Web research is
allowed; future solution lookup is prohibited by instruction. The small completion
report includes a **self-reported** `historical_solution_seen` answer, not anti-cheating
proof. New frozen revisions bind their task statement in `task-instructions.json`;
older revisions use the registered statement, copied and hashed into each run identity.
Tree-only legacy revisions cannot run benchmarks until reimported/frozen with history.

`completed` means the harness exited successfully with a valid completion report;
it does not mean the task passed. `crashed`, `timeout`, `cancelled` and `error` describe
execution outcomes. Infrastructure/output collection failures are errors with no task
verdict. Ctrl-C and CLI SIGTERM remove the container and retain a safe partial tree
when possible. CLI exit codes are 0 for completed, 124 for timeout, 130 for cancellation,
and 125 for run errors/crashes. After SIGKILL or host failure an identity can remain
`running`; explicitly seal it with `bench recover TASK RUN` after checking the runner
has exited. Recovery takes the task lock, records `unclean_host_exit`, and never reruns
an agent or claims a task failure. Forced host termination can leave an orphan container
or disposable staging directory; inspect managed Docker resources before recovery.

Only the final tree archive, small completion/status report and minimal operational
provenance survive normal execution. No transcripts or raw session histories. Export
creates a new tree-only directory outside the store; it includes added/deleted/modified
files as the saved final state, without agent-controlled Git. Safe outputs from crashes,
timeouts and cancellation are retained too. Outputs can contain secrets copied by an
agent: inspect code before sharing. Unsupported links/special files fail collection.

Comparisons take explicit left/right lists of saved task/run IDs, one attempt per task
and one model + harness per side. They report the exact **common revisions**, coverage,
and left/right-only revisions, including zero overlap. Different revisions of the same
task do not overlap. Execution failures stay visible in coverage; no task grading or
aggregate scores are produced in this phase. Task/suite/run identities remain immutable.
See [decisions](docs/mineval-decisions.md) for the boundaries and deferred choices.

## Import an externally selected task

Edit [candidate.json](examples/candidate.json), then:

```sh
mineval --data mineval-data import candidate.json
```

Ask a selection agent to read the portable
[Moment Selector skill](skills/mineval-moment-selector/SKILL.md) in any Git project,
for example: “Use Mineval and create ideal benchmark tasks from this project.” Copy
the skill folder to another project or skill directory as needed. It preserves the
detailed fixup-chain, root-cause, feasibility and evidence heuristics from the original
[selector entry point](MINEVAL_MOMENT_SELECTOR.md). Discovery is external to Mineval.

The agent writes ordinary candidate JSON, then runs `mineval --data /private/store
task add /path/to/candidate.json`. It never arranges internal store directories.
Required record fields are nonempty `repository` (local identity or URL) and an
outcome-only `task_summary`. Accepted imports also need a local `base_source`, full
`base_commit`, and `head_commit` (legacy `future_commits` is supported). Source paths
are relative to the candidate JSON file, not the shell's working directory.

Record `related_commits`, `related_prs`, `why_high_signal`, `future_evidence`,
`execution` (runtime, likely commands, external requirements, cleanup risks),
`verification_feasibility`, and `uncertainty`. Each evidence entry has a full `commit`,
`path` and `proves`; use concrete `reproduction` instructions instead of `path` when
no file exists. Intake captures related/evidence commits as well as head, checks base
ancestry and evidence file existence, and preserves notes privately. It does not
execute code or establish whether those tests are sufficient. Evidence may follow head.

Use `selection_status: "defer"` and list unknowns rather than inventing missing facts.
Deferred or missing-boundary records have `needs-review` state with no draft/source
export; `selection_status: "reject"` creates a rejected record. Both remain inspectable.
Malformed supplied fields and invalid accepted imports fail atomically. Correct a
deferred record by adding a new explicitly identified candidate and noting the prior
ID; no in-place resume is implemented. Legacy records missing detailed evidence notes
still import as drafts, with `intake.status: "incomplete"` and explicit missing fields.
Draft intake is never preparation approval. IDs do not deduplicate repository aliases.
PR URLs alone cannot supply historical objects: provide local commits. No GitHub API,
network, crawler or live model is needed.

The deterministic [discovery fixture](tests/test_discovery.py) builds a single-request
API, a batch feature, an empty-batch fix, and later regression tests. It proves the
check fails before the fix and passes afterward, imports base/head/later evidence via
`task add`, and checks that only base files enter the draft. Future trees and diffs
remain private and survive removal of the source repository.

Two source modes are supported:

- `base_source: {"type": "git", "path": "/local/repository"}` reads exact Git blobs,
  checks commits and base ancestry, and retains the chosen future trees/diffs privately.
  Supply a local clone yourself for remote repositories. No checkout, Git hook, textconv,
  solution execution, or repository discovery runs during import.
- `base_source: {"type": "tree" | "archive", "path": "..."}` accepts a raw export.
  Supply `future_source` using the same tree/archive types; it contains directories
  named by future commit SHA, with evidence at its original relative paths. Include
  `source_attestation` explaining how the producer obtained this historical material.
  Raw historical identity is **producer-attested**, not independently Git-verified.

Archives may be tar or tar.gz. The current tree-only importer requires no `.git`, links, special files,
or submodules; unsupported candidates fail explicitly. Each imported collection is
limited to 512 MiB and 100,000 files, with at most 50 future commits. The original
candidate and SHA-256 inventories are retained. Registration is atomic with respect
to normal failures; failures remove partial imports. Mineval does not fetch a replacement.

## Clean and diagnose a draft

Imported base files are in `drafts/<task>/workspace`; host-managed controls are in
`drafts/<task>/control`. Fill in `control/verify.json` using [verify.json](examples/verify.json).
The initial configuration intentionally has no image or commands: choose real checks.
Commands use argv, with explicit `/bin/sh -c` when needed. Allowed kinds are `setup`,
`build`, `tests`, and `smoke`. Each has a timeout bounded by the overall diagnostic
deadline. CPU, memory, PID, and temporary-memory limits are configurable.

Launch any explicit preparation executable from the host:

```sh
mineval --context rootless --data mineval-data prepare external-task-001 -- /path/in/image/to/executable arg
```

The executable receives a disposable copy of the current draft at `/workspace`,
freshly reconstructed pre-base Git history, disposable `/mineval-control`, read-only
`/mineval-input/prompt.txt` with task-preservation instructions, and `/mineval-broker`
(a dedicated Unix socket). No future material, host repository, private store or
Docker socket is mounted. The cleaner must follow those instructions and write
`/mineval-control/cleanup-report.json`, following [cleanup-report.json](examples/cleanup-report.json).
Agent process logging is disabled; no transcript is retained.

Alternatively use the Phase 3 adapters (image comes from `verify.json`):

```sh
mineval --context rootless --data mineval-data cleanup external-task-001 --adapter fake
# Explicit opt-in to a real model, with only copied model authentication:
mineval --context rootless --data mineval-data cleanup external-task-001 --adapter codex --model gpt-6-luna --auth-file /path/to/auth.json
```

The fake cleaner makes no edits and invokes a fresh diagnostic. Codex needs an image
with its CLI, Bash, Git, project runtime and the Mineval client. Explicit commands
work with legacy raw exports; those have no Git history. The fake adapter requires
a Git import. Ordinary outbound access is enabled for cleanup; future-solution lookup
is prohibited by instructions, as in Phase 3.

After termination the host discards agent Git metadata and publishes the validated
tree and diagnostic configuration back to the draft. Each attempt saves a separate
`tasks/<task>/cleanups/<id>/` archive, tree hashes, binary patch, environment/config,
producer identity and report with hashes. Changes are compared to the original base,
including changes from earlier attempts; every path needs a reason. Runtime,
dependency versions, supporting services and limitations must be recorded.
`task show` exposes the latest cleanup result. `ready-for-review` is a producer claim,
pending independent review. Missing/invalid reports, stale diagnostics, environment
failures or process failures produce `needs-review`; a producer may explicitly choose
`needs-review` or `reject` when setup would alter the task or need unsafe infrastructure.
These dispositions stay on the preparation attempt; use `reject` to reject the task.
Safe partial edits are retained for subsequent cleanup attempts. Unsupported output
links/special files fail collection and leave the prior draft intact.

While that session remains running, the agent can edit the workspace and controls,
and invoke repeatedly:

```sh
mineval verify
mineval verify report
```

The socket serves **only this task's** verify/latest-report requests. The host pauses
the preparation container briefly, copies its current workspace and configuration, then resumes
it before checking the copy in a new sandbox. Setup/build/tests share this fresh
sandbox so setup effects persist within a diagnostic, then all changes are discarded.
The original draft and preparation process remain available. Direct host checks are
also available when there is no active session:

```sh
mineval --context rootless --data mineval-data verify --task external-task-001
mineval --data mineval-data verify report --task external-task-001
```

Reports contain input hashes, the exact configuration, resolved image ID, container
ID, command statuses, exit codes, stdout/stderr, durations, timeout/OOM indicators,
and truncation warnings. Statuses distinguish environment startup/control errors,
command errors (reserved exit codes 126/127; 125 denotes a Docker control error), workload failures, passed commands,
timeouts, and skipped commands. These reserved codes follow Docker's command-status
convention; do not use them as ordinary test-failure codes. A timeout kills the entire
verification container and skips later steps. Other test failures remain diagnostic.
Setup/build failures and missing commands also skip later checks and yield
`environment_error`. Each command has an `interpretation`: `passed`,
`environment_failure`, `behavior_failure`, or `expected_task_failure`. To annotate
a known historical failure, add `"expected_failure": "reason the base should fail"`
to a `tests` command. This records a claim for review; unexplained failures remain
behavior failures, and an expected-failure annotation never excuses infrastructure
errors. Diagnostics reconstruct trusted pre-base Git afresh and cannot change the draft.
The CLI returns 0 for completed checks, 1 for failed checks, 125 for environment errors.
It never interprets log text or rejects a task because tests fail.

Diagnostic `network` is configurable as `bridge` (default) or `none`; cleanup agents
use `bridge`. Host networking
is rejected. Network access does not provision external services or credentials.
The root filesystem is read-only; install project dependencies into the workspace
or choose a suitable preloaded image. Do not add secrets to controls or code.
The session and each diagnostic have independent deadlines; allow enough session
time for preparation and repeated checks.

## Review and freeze

Cleanup may only accommodate the execution environment. Review all changes against
the preserved base. If cleanup would solve, bypass, remove, or substantially change
the target work, reject the candidate:

```sh
mineval --data mineval-data reject external-task-001 --reason 'Cleanup would change the objective'
```

After a completed cleanup, run a separate extraction agent and a third review
session. These are independent operations, with fresh disposable containers. Explicit
executables receive instructions at `/mineval-input/prompt.txt` and write structured
reports under `/mineval-control`. They never mount the store or change the draft.

```sh
mineval --context rootless --data mineval-data verification-extract external-task-001 --command /path/in/image/to/extractor
# Use the returned extraction_id; config uses the same schema as verify.json.
mineval --context rootless --data mineval-data verification-check external-task-001 --extraction-id EXTRACTION --config private-checks.json
mineval --context rootless --data mineval-data verification-check external-task-001 --extraction-id EXTRACTION --config private-checks.json --future-commit FULL_SHA
mineval --context rootless --data mineval-data review external-task-001 --extraction-id EXTRACTION --command /path/in/image/to/reviewer
mineval --data mineval-data finalize external-task-001 --review-id REVIEW
```

For either producing/review session, replace `--command ...` with
`--adapter codex --model gpt-6-luna --auth-file /explicit/auth.json` for an explicitly
authorized model run. The existing Codex adapter uses a fresh ephemeral session and
copied model authentication; no transcript logs are retained. Local fake executables
can follow the same report contract without a model. No automatic pipeline or human
sign-off is required.

The extraction report follows [verification-plan.json](examples/verification-plan.json):
choose focused meaningful checks or a fuller package, explain the requirements,
record dependencies, running instructions and limitations, and select original files
with commit/path provenance and `test_only: true`. `unavailable` plus an empty artifact
list records a needs-review extraction attempt when credible checks cannot be recovered.
This cannot become approved verification. The host copies checked original bytes;
agent edits to its private source copies are discarded.

`verification-check` overlays selected original files at their original paths in a
disposable cleaned base or known future tree and runs commands in a fresh sandbox.
It preserves command outcomes, input/config/image identity and setup versus behavioral
failures privately, without replacing the public cleanup diagnostic. A base behavior
failure can be legitimate; a future pass supports the checks without declaring the
historical patch the only correct implementation. Future checks use the original
future tree without cleanup adaptations; reviewers must assess any resulting setup
limitations. Validation is optional where infeasible, and limitations require review.

The reviewer writes [review.json](examples/review.json), returning approve, revise or
reject with reasons. It assesses every cleanup edit, leakage, test relevance, arbitrary
assumptions and reproducibility. Approval requires objective/snapshot attestations
and one reason for exactly each changed path. These remain semantic judgments;
Mineval cannot mechanically prove meaningful tests or detect all leaked secrets.
A revise/reject report blocks freezing with that review ID but preserves the task
for further preparation; use `reject` to reject the task itself.

Finalization requires an approved independent session ID and a current runnable
cleanup diagnostic. Inline `finalize --review` attestations no longer freeze new
versions. A matching ready-for-review cleanup and session-produced extraction are
mandatory. Review receipts bind the exact cleanup, extraction, diagnostic, metadata
and private validation reports. Changed inputs require another review. Failing
historical tests remain allowed; infrastructure-only diagnostics cannot publish.
Active sessions hold the task lock. Stop external editors during host operations.

Every finalization creates a new version containing `snapshot.tar.gz`, its SHA-256,
the exact tree inventory, a binary-capable cleanup diff, review, configuration, last
diagnostic, private verification/validation, independent review, provenance, and hashes. The archive contains only the reviewed workspace. Git history,
controls, provenance, future material, and extraction output remain outside it.
Archive ownership/timestamps are normalized. Version files become read-only and are
never overwritten through Mineval. Hash checks detect subsequent host tampering;
this is application-level immutability, not protection from the owning OS user.
New versions declare their tree/history format in `workspace-format.json`; legacy
versions remain readable. New Git imports save a sanitized base-history pack; new
revisions include it and materialization reconstructs historical .git. Existing
revisions and raw exports remain tree-only. Reimport complete Git sources and publish
new revisions to use the isolated agent runner. See
the [storage format and migration boundary](docs/foundation.md).

## Extract future verification privately

The legacy generic extraction session can inspect the base, explicit future trees,
diffs (Git imports), and task context:

```sh
mineval --context rootless --data mineval-data extraction-session external-task-001 -- /path/in/image/to/executable arg
```

Its private review workspace is disposable. It can write `selection.json` at the
workspace root; Mineval retains that suggestion in `private/extraction-selection.json`.
This legacy path cannot supply the session-produced verification required for new freezing.
No selection is automatically applied or executed. Alternatively inspect supplied
future evidence with your own external process and provide a selection directly.

Review [selection.json](examples/selection.json), then explicitly preserve the checks:

```sh
mineval --data mineval-data extract external-task-001 --selection selection.json
```

Each entry names a known commit, original path, purpose, and `test_only: true`
attestation. Include supporting fixtures/helpers/test dependencies as separate entries.
The host validates paths and source hashes, copies original bytes into a new private
extraction, and records provenance/hashes. `test_only` requires reviewer judgment;
file extensions cannot reliably distinguish tests from production. Never select
future production implementations as test support. A future production API referenced
by a test remains work for the evaluated agent. These manually selected artifacts remain inspectable but cannot replace an independent
extraction session for new freezing. Extracting verification leaves every
frozen version unchanged and never runs checks against a candidate solution.

## Workspaces and future harness boundary

```sh
mineval --data mineval-data workspace external-task-001 VERSION runs/run-001/workspace
mineval --data mineval-data workspace external-task-001 VERSION runs/run-002/workspace
```

Each destination must be new. Mineval verifies the archive and extracted hashes.
The resulting writable directories are independent, and the archive is never mounted.
A future harness can bind only its own working copy into `SandboxSpec(workspace=...)`.
Do not mount a version directory or share workspaces between runs.

The existing API exposes create/destroy through `with DockerSandbox().container(spec)`,
plus `start`, `exec`, `inspect`, `logs`, `wait`, `result`, and `export_workspace`.
Workspace export pauses a running container, copies its result, and returns an archive
hash. The orchestration layer chooses the command; no agent CLI or grading protocol
is prescribed. See [architecture and limitations](docs/foundation.md).

## Checks

```sh
python -m unittest discover -s tests -v
MINEVAL_TEST_CONTEXT=rootless MINEVAL_TEST_IMAGE=YOUR_PRELOADED_SHELL_IMAGE python -m unittest discover -s tests -v
```

The normal suite uses real temporary Git history and simulated Docker lifecycle results.
It covers intake, leakage boundaries, archives, reviews, hashes, extraction, rejection,
independent workspaces, broker requests, diagnostics, deadlines, isolation arguments,
and cleanup failures. Live rootless checks are opt-in. The preparation/cleanup checks
need `/bin/sh`, `sleep`, Python 3, Git and this checkout's Mineval package in the image;
they never pull an image or modify daemon setup. Set `MINEVAL_AGENT_TEST_IMAGE` to
enable the separate live fake-adapter check as well.

## Phase 3: isolated agent primitive

Run from the Mineval repository root. The input is a **materialized historical
workspace**, not your live checkout. `agent-run` reads it and mounts only a temporary
copy at `/workspace`. The agent process runs entirely inside rootless Docker;
there is no host execution fallback.

New Git intake retains exactly the objects reachable from the task base, including
merge parents. A fresh Git repository is reconstructed and every stored object is
checked against that closure. Future refs/tags, dangling objects, reflogs, remotes,
hooks, configuration and alternates are not copied. Shallow sources fail closed.
Pre-base content/messages remain visible unchanged. Preparation drafts and tree-only
archives retain the existing codec; materialization attaches history separately,
leaving preparation edits as working-tree changes.

Use a trusted preloaded image with the project's runtime/dependencies.
[Dockerfile.agent](examples/Dockerfile.agent) is an explicit recipe with Git, Python
and Codex 0.163.0. That npm-based recipe remains unverified because Debian mirror
downloads stalled. The alternate [native image recipe](examples/Dockerfile.agent-native)
uses Python/Alpine plus Git, Bash and copies of the standalone Codex executable
and its code-mode companion. Codex 0.164.0 was verified inside the alternate image.
The host CLI is a launcher; neither it nor host installation/home/configuration is
mounted. Compatibility is specific to the tested static Linux x86_64 binaries; never copy
a host launcher and assume it is portable.

```sh
# Explicit build under an already configured rootless context.
docker --context YOUR_ROOTLESS_CONTEXT build -f examples/Dockerfile.agent -t mineval-agent:local .

# Substitute the exact task/revision from import/preparation.
python -m mineval --data mineval-data workspace TASK VERSION /tmp/mineval-historical
# Write the outcome-only task statement to /tmp/mineval-task.txt.
python -m mineval --context YOUR_ROOTLESS_CONTEXT agent-run /tmp/mineval-historical \
  --instructions /tmp/mineval-task.txt --output /tmp/mineval-fake-result \
  --image mineval-agent:local --adapter fake

# Paid execution: only when deliberately authorized; this user's allowed model is Luna.
python -m mineval --context YOUR_ROOTLESS_CONTEXT agent-run /tmp/mineval-historical \
  --instructions /tmp/mineval-task.txt --output /tmp/mineval-luna-result \
  --image mineval-agent:local --adapter codex --model gpt-6-luna \
  --auth-file /explicit/path/to/dedicated-auth.json --timeout 60
```

Output paths must be new. The fake adapter writes a predictable marker and never
contacts a model; its Python API also supports error/timeout fixtures. Bridge outbound
networking is enabled. Instructions forbid looking up the source project's future
solution; this is a trust policy, not a technical guarantee.

Codex receives task text on stdin, uses ephemeral state without user config/rules,
and writes a short schema-constrained report. Container logging is disabled for this
runner; stdout/stderr are discarded and temporary Codex state is destroyed. Results
contain status/exit/timeout/OOM fields, a summary, and a **self-reported** solution-lookup
declaration. Missing/malformed completion reports become errors. Interruption removes
the container and temporary files; cleanup failures remain visible.

Authentication is explicit: `--auth-file` copies only allowlisted Codex auth JSON
into a temporary read-only input mount, then into tmpfs. Credentials are never read
or inherited automatically. Use dedicated model-only credentials; never include
GitHub/push credentials, unrelated secrets or user configuration in the image/input.
The agent can read and potentially exfiltrate this model credential. Subscription
inference and GPT 6 Luna access were verified on this machine; token refresh
remains unverified. If setup cannot work safely, fix it;
do not mount your home or run on the host. See
[official authentication guidance](https://developers.openai.com/codex/auth).

Only `result.json` and a bounded link-free `workspace.tar.gz` are retained. Agent Git
metadata is discarded without executing it on the host. File changes from failed or
timed-out work are retained too. Review outputs for secrets before sharing; an agent
can copy any visible credentials into code. Bind workspace disk usage has no hard
quota; exported artifacts use the existing byte/file limits. The `agent-run` primitive does not create benchmark records; use `bench run`
for durable attempts tied to frozen revisions and suites.

Deterministic checks use temporary synthetic Git repositories and simulated Docker:
`python -m unittest discover -s tests -p test_agents.py -v`.
Set `MINEVAL_AGENT_TEST_IMAGE` to a trusted preloaded Git/Python image and optionally
`MINEVAL_TEST_CONTEXT` for an explicit live **fake-only** check. This never invokes
Codex. See the [Phase 3 handoff](docs/phase-handoffs/phase-3.md) for actual live
Docker and Luna smoke-test results.

To build the native alternative, use a temporary context containing only the recipe
and the two compatible standalone executables (no user configuration or credentials):

```sh
TASK_IMAGE_CONTEXT=$(mktemp -d /tmp/mineval-image-XXXXXX)
cp examples/Dockerfile.agent-native "$TASK_IMAGE_CONTEXT/Dockerfile"
# Replace this directory with the installation you have inspected.
cp /home/relic/.local/share/mise/installs/codex/0.164.0/bin/codex "$TASK_IMAGE_CONTEXT/"
cp /home/relic/.local/share/mise/installs/codex/0.164.0/bin/codex-code-mode-host "$TASK_IMAGE_CONTEXT/"
docker --context mineval-rootless build -t mineval-agent:phase3-ready "$TASK_IMAGE_CONTEXT"
rm -rf -- "$TASK_IMAGE_CONTEXT"
```

The context above includes binaries only. Authentication is provisioned at run time,
never during image building. The native recipe checks Bash, Git, Python and Codex at
build time; project-specific runtimes still need to be added explicitly.
