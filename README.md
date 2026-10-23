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
contain the `mineval` client (see [example Dockerfile](examples/Dockerfile.preparation)).
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
```

`task add` uses the existing import path (`import` remains supported). Task IDs are
caller-supplied and persistent; duplicates fail. Inspection returns readable JSON.
Prepared revisions use the existing immutable `versions/<version>` artifacts created
by `finalize`; revision inspection checks their integrity receipts. `prepare` still
requires an explicit executable and does not select or call a model automatically.

Use [suite.json](examples/suite.json) for an explicit ordered list of task/version
pairs. Suite creation checks each prepared revision and pins its snapshot and artifact
manifest hashes. There is no implicit latest version or live pool query. A suite ID
cannot be overwritten; publish a new ID to change membership. Adding tasks or preparing
new versions leaves existing suites unchanged. Suite inspection checks the definition's
receipt; it does not revalidate the referenced task artifacts or current eligibility.

`mineval bench run [TARGET]` and `mineval evaluate [RUN_ID]` are reserved independent
operations and exit 2 with a clear not-implemented error, without creating records or
contacting Docker. The existing `run` command is only a generic sandbox utility.
Task-local `runs/` and `evaluations/` directories reserve record namespaces; no benchmark
execution, evaluation, or adapter protocol is implemented in this phase. Existing data
stores remain readable without a migration. See [decisions](docs/mineval-decisions.md)
for the intended boundaries and deferred choices.

## Import an externally selected task

Edit [candidate.json](examples/candidate.json), then:

```sh
mineval --data mineval-data import candidate.json
```

Required fields: `task_id`, `repository` (identity), `task_summary`, full `base_commit`,
and explicit `future_commits` and/or `head_commit`. PR references and candidate notes
are retained when supplied; absent notes remain unknown. PR URLs alone cannot supply
historical objects: ask the external producer for the corresponding commit SHAs.
No crawler, selector, ranking agent, queue, or producer output directory is required.
[MINEVAL_MOMENT_SELECTOR.md](MINEVAL_MOMENT_SELECTOR.md) is standalone external guidance.

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

Archives may be tar or tar.gz. Sources must have no `.git`, links, special files,
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

This is a generic command boundary, not a model integration. The executable receives
only the draft workspace, `/mineval-control` (configuration and task summary), and
`/mineval-broker` (a dedicated Unix socket). No future material, host repository,
private store, arbitrary host paths, or Docker socket is mounted. Agent-specific
prompt delivery belongs to the caller. stdout/stderr are returned after the session;
this CLI does not implement an interactive agent terminal.

While that session remains running, the agent can edit the workspace and controls,
and invoke repeatedly:

```sh
mineval verify
mineval verify report
```

The socket serves **only this task's** verify/latest-report requests. The host pauses
the preparation container briefly, copies the draft and configuration, then resumes
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
The CLI returns 0 for completed checks, 1 for failed checks, 125 for environment errors.
It never interprets log text or rejects a task because tests fail.

`network` is configurable as `bridge` (preparation default) or `none`; host networking
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

To publish a version, create a review like [review.json](examples/review.json). Give
exactly one nonempty reason for each changed, added, or deleted path (`changes: []`
for an unchanged tree). `objective_preserved: true` attests the target behavior and
interfaces remain intact. `snapshot_reviewed: true` attests inspection for future
solution/tests, benchmark-private material, and credentials. These are substantive
review obligations: Mineval cannot prove semantic non-leakage or recognize every secret.

```sh
mineval --data mineval-data finalize external-task-001 --review review.json
```

Finalization requires a diagnostic of the exact current tree/configuration in which
at least one command ran. Failing historical tests are allowed. Infrastructure-only
failures and stale diagnostics cannot publish a version. Active sessions hold a task
lock, preventing concurrent finalization or another host preparation operation.
Stop external editors before host checks/finalization; inconsistent copies are rejected.

Every finalization creates a new version containing `snapshot.tar.gz`, its SHA-256,
the exact tree inventory, a binary-capable cleanup diff, review, configuration, last
diagnostic, and hashes. The archive contains only the reviewed workspace. Git history,
controls, provenance, future material, and extraction output remain outside it.
Archive ownership/timestamps are normalized. Version files become read-only and are
never overwritten through Mineval. Hash checks detect subsequent host tampering;
this is application-level immutability, not protection from the owning OS user.

## Extract future verification privately

A separate generic extraction session can inspect the base, explicit future trees,
diffs (Git imports), and task context:

```sh
mineval --context rootless --data mineval-data extraction-session external-task-001 -- /path/in/image/to/executable arg
```

Its private review workspace is disposable. It can write `selection.json` at the
workspace root; Mineval retains that suggestion in `private/extraction-selection.json`.
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
by a test remains work for the evaluated agent. Extracting verification leaves every
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
and cleanup failures. Three live rootless checks are opt-in and require `/bin/sh` and
`sleep` in the selected image; they never pull an image or modify daemon setup.
