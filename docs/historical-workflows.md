# Historical benchmarking from the web and CLI

Run `mineval --context YOUR_ROOTLESS_CONTEXT --data DATA observe --image YOUR_PRELOADED_PREPARATION_IMAGE --auth-file /explicit/model-only/auth.json`.
The printed secret loopback URL opens the benchmark overview. The image must
already exist and contain Git, Python, Mineval and the isolated Codex executable
(and the dependencies the project needs). No images are pulled. The browser never
receives the auth path, credentials, host command execution or Docker APIs.
Web Arena continues to use its separate server, store, execution path and policies;
its embedded historical pages remain read-only. Use `observe` for historical actions.

The **Tasks** workspace lists every repository and supports repository, category, state,
prepared/unprepared and text filters. Check task rows to reveal preparation, benchmarking and removal actions above the table. Task filters explicitly clear selection; selection is limited to the visible table. Choose **Select all visible** for bulk selection.
Each prepared row lets you select an exact frozen revision (the current frozen
revision is selected initially). **Prepare all** is always available above the table and opens a confirmation showing eligible and skipped tasks plus the effective preparation agents. It considers eligible unprepared tasks
across all repositories, independently of the visible filters. Removal means
confirmed retirement, not erasing historical evidence; there is no restore command.
Existing task detail, revision, run, assessment and evidence pages remain available.

Preparation runs cleanup, fresh diagnostics, separate private extraction, private
base/future validation when runnable commands are available, a third independent
review session and freeze. Sessions do not share model state. A rejection or lack of
credible verification is a per-task failure, never implicit approval. New drafts
receive only a bootstrap runtime diagnostic from the selected image; cleanup must
replace it with meaningful repository checks. Runtime settings for an existing valid
draft are preserved. Validation failures are retained for the independent reviewer;
a historical base failing a behavioral check is not a reason to reject automatically.

Choose one or several discovered model/effort choices (or saved host presets), or enter explicit model identifiers
(one per line) and an effort. Run automatically evaluates the saved candidate,
records a factual model observation and, for Codex attempts by default, runs a
separate qualitative review. Compaction is independent maintenance. The fake adapter is available
through settings/CLI for fixtures; its reviews are factual only. It never calls a model.

## Application navigation and benchmark configuration

The persistent sidebar opens Overview, Tasks, Benchmarks, Models, Activity,
Web Arena and Settings. Overview shows real evaluated configuration summaries,
coverage, the fixed engineering-category matrix and recent queue activity.
All-evidence statistics remain descriptive; comparable results use only the exact
common revision and evaluation-condition intersection. Empty data is unavailable,
never a fabricated percentage.

**Run benchmark** in Overview or Benchmarks opens a dedicated three-step workspace:
choose prepared tasks, choose models being benchmarked, then review and run.
Starting from task selection preselects eligible tasks and identifies exclusions.
Task filters in the wizard preserve selected tasks, including those outside the
visible results. Model discovery lists supported reasoning efforts as independent
checkbox configurations, so the same model can be tested at several efforts.
Explicit identifiers remain available under advanced disclosure.
The review step pins exact revisions, checks private evaluation configuration and
previews already-attempted combinations before confirmation. With no runnable
combinations the launch action is disabled. Queue submission revalidates all inputs.
The skip option matches complete revisions, model, effort and harness, including
unsuccessful attempts. Automatic private evaluation remains part of every run.

Preparation and benchmark submission open Activity with per-task submission outcomes,
durable stages, saved result links and appropriate retry actions. Activity has state
filters; refresh retrieves current progress. Settings expose effective preparation,
AI-assisted evaluation and qualitative review roles, including read-only host review
overrides. Private checks themselves do not require an AI model. Settings changes
apply only to newly queued jobs; the existing model snapshots and retry semantics
are preserved.

Task details organize preparation, frozen verification, evaluations and diagnostics
behind section links and artifact disclosures. Selecting a model opens a dedicated
profile with per-effort performance, category breakdowns, accumulated observations,
source provenance and historical runs, rather than appending details to the list.
The Arena workspace uses the same dark visual system; its historical pages remain
read-only and explain how to open the trusted operational observe server.

## Model settings and Codex discovery

Open **Settings → Agents & models** in the historical dashboard to select the **Prep / extract** and
**Evaluation** model and reasoning effort. Prep applies to cleanup, extraction and
independent preparation review. Evaluation applies to the evaluator, qualitative
review. Memory compaction uses the separate maintenance queue. The trusted host `review_model` / `review_effort`
settings can override the review sessions, and are called out on the settings page.

Choices are saved atomically in `DATA/historical-model-settings.json` and override
the corresponding host defaults for web and CLI workflow queues. Delete that file
to return to host defaults. New jobs snapshot these choices; changing settings
leaves existing queued jobs and their retries unchanged. Legacy jobs without a
snapshot resolve the current choices when executing.

Following Aproekt, Mineval starts the host `codex app-server --stdio`, performs
`initialize` / `initialized`, and pages through `model/list`. Discovery performs no
inference. It lists visible models, their supported reasoning efforts and defaults;
the benchmark configuration picker uses these models when no host `models` presets are configured.
Results are cached for five minutes (failures for fifteen seconds). **Refresh Codex
models** bypasses the cache. Explicit identifiers remain usable on discovery failure
or for models outside the visible catalog.

Discovery uses an isolated temporary Codex home, copying only the explicit
`--auth-file`, or the local Codex credential file when no explicit file is given.
It does not load host rules, plugins or configuration. Discovery requires a host
Codex CLI; execution still requires Codex in the trusted preloaded Docker image
and the explicit `--auth-file` when credentials are needed. The host catalog does
not prove that an older image runtime supports every discovered model or effort.

## Gretel preparation runtime

`examples/Dockerfile.gretel` provides Node.js 24, npm, Python, Git, native addon
build tools, the Mineval diagnostic client, and the standalone Codex binaries from
the preloaded `mineval-agent:phase3-ready` image. It contains no credentials
or Gretel source. Build from the repository root with a minimal context:

```sh
GRETEL_BUILD_CONTEXT=$(mktemp -d /tmp/mineval-gretel-build-XXXXXX)
cp examples/Dockerfile.gretel "$GRETEL_BUILD_CONTEXT/Dockerfile"
cp pyproject.toml "$GRETEL_BUILD_CONTEXT/"
cp -r mineval "$GRETEL_BUILD_CONTEXT/"
docker --context mineval-rootless build -t mineval-gretel:node24 "$GRETEL_BUILD_CONTEXT"
rm -rf -- "$GRETEL_BUILD_CONTEXT"
```

Set `preparation_image` in `DATA/historical-settings.json` to this image (or its
resolved image ID). Start the dashboard with `--context mineval-rootless` before
`observe`, and `--auth-file /explicit/model-only/auth.json` after it. Runtime
settings are loaded at startup, so restart an existing dashboard after changing
them. Failed jobs remain failed until explicitly retried.

Benchmark output collection removes generated `node_modules` directories from the
candidate snapshot when the starting tree contains no files beneath them. This
keeps npm's links and installed packages out of the saved artifact; task source
should live outside generated dependency directories. If the starting tree contains
source under such a directory, it is retained and normal link and size validation
still applies. Links and special files elsewhere remain forbidden. A collection
exception is recorded in the restricted diagnostic stream, not benchmark receipts.

## Reusable dependency downloads

Historical preparation, diagnostics, review, benchmark runs and evaluation can
reuse immutable upstream downloads under `DATA/download-cache/artifacts`. Images
opt in to the managers they contain:

```dockerfile
LABEL io.mineval.dependency-cache="npm,pip,go,cargo"
```

The legacy `io.mineval.npm-cache="v1"` label still enables npm. Omit both labels to
disable caching. Opting in does not install missing package managers into an image.
Each container gets private writable copies of only its authorized dependencies;
the shared store is never mounted. Verified immutable staging seeds, extracted Go
modules, Cargo vendor sources and eligible wheel installations are reusable, but
no writable inode or agent output is shared between tasks. Virtual environments,
node_modules, Cargo targets and task build outputs remain isolated. Codex's cleared
environment preserves these cache locations,
the image's tool PATH and its Rust toolchain location (`RUSTUP_HOME`).

Authorization comes from historical manifests and lockfiles, including nested
projects. Benchmark runs and evaluations use the receipt-checked, independently
approved frozen snapshot, including approved dependency changes. Preparation
uses the preserved historical base, so new agent-written dependencies are downloaded
normally until approved. Symlinked files/directories and generated directories
(`node_modules`, `vendor`, `target`, `.venv`, `venv`, `.git`) are excluded.

| Manager | Supported inputs and upstream sources | Isolated installation |
| --- | --- | --- |
| npm | Root/nested `package-lock.json` (v1–v3), `npm-shrinkwrap.json`; public npm registry HTTPS tarballs with SHA-256 or SHA-512 SRI | Native npm content cache at `/mineval-npm-cache`; use `npm ci` |
| pip | Pinned SHA-256 requirements, recursive `-r` includes, `uv.lock`, `poetry.lock`, `Pipfile.lock`, `pylock.toml`; PyPI wheels/source archives and hashed direct `files.pythonhosted.org` wheels | `PIP_FIND_LINKS=/mineval-dependency-cache/pip/packages`; eligible hashed wheel environments are cloned privately; other installs use native pip |
| Go | Root/nested `go.mod` + `go.sum`; recursively follows checksum-verified required module metadata from `proxy.golang.org` | Private `GOMODCACHE`, including verified pre-extracted modules; native Go selects versions |
| Cargo | Root/nested `Cargo.lock`; crates.io archives with locked SHA-256 checksums | Private native Cargo archive cache; an optional directory source supports offline setup; build targets stay private |

Python lockfiles authorize all locked platform wheels; pip selects compatible
Python/ABI/platform tags. Downloaded source distributions are reusable, but locally
built wheels and build-backend dependencies are not shared. Use `pip install
--require-hashes -r requirements.txt` with a complete exported requirements file;
add `--no-index` for an intentionally offline installation. A lockfile is an
acquisition input, not an instruction for pip to install it directly. uv/Poetry
commands that ignore `PIP_FIND_LINKS` must explicitly use the staged find-links
location or export requirements for pip. pip may still query index metadata.

Go's graph is rooted in `go.mod`, never every old entry in `go.sum`. Unreachable
sum entries do not authorize artifacts. Go modules use the standard h1 hash of
module files, not a raw ZIP checksum. Module path case escaping and exact versions
are preserved. Missing historical transitive checksums, replace/exclude directives,
private proxies, local replacements and toolchain downloads fall back to normal
Go resolution and are reported. Native Go remains responsible for version selection.

Cargo seeds `.crate` archives into its private native cache. Its mutable
registry index is not shared and may still download. Known stable/legacy cache
identities are supported; images using another Cargo layout should declare
`LABEL io.mineval.cargo-cache-id="index.crates.io-1949cf8c6b5b557f"` with the actual
registry/cache directory name produced by that image's Cargo version. This identity
is recorded in receipts. A different, unrecognized layout may redownload archives.

When the complete crates.io set can be staged safely, Mineval also generates an
optional Cargo directory source with per-file checksums. For an explicitly offline
setup command, use `cargo --config
/mineval-dependency-cache/cargo/vendor-config.toml build --frozen`. This replacement
is not activated automatically, so an agent can add dependencies and use normal
Cargo resolution. Git/alternate registry dependencies are reported and use normal
Cargo resolution. Incomplete or invalid crates.io sets omit the optional vendor
configuration. Build scripts may download their own resources, subject to the
existing sandbox network policy.

Only explicitly allowlisted public HTTPS services are contacted; host proxies,
credentials and cookies are not used. Host acquisition performs bounded HTTP
requests and integrity checks, never executes package managers, build backends,
project scripts or Git on the host. `network=none` prevents all host acquisition;
cache misses remain misses. Bridge-enabled tasks may fetch missing authorized
archives before container startup. Acquisition uses a lock per artifact; identical
cold requests fetch once while unrelated artifacts can download concurrently.
Manifest parsing, snapshot extraction, source expansion and staging are reused
through receipt-checked immutable seeds. Cache misses are not persisted as successes.
Entries are verified before reuse and published atomically; failures never publish
partial data.

Agent-written cache data is never imported into this store. The only installation
output accepted is from Mineval's separate offline, credential-free wheel installer.
It accepts pinned hashed requirements and local includes, installs only wheels with
`--no-deps`, and has no task source, direct URLs or build hooks. Its key includes the
immutable image ID, wheel bytes and requirements; native pip checks Python/ABI tags
and markers. Complete exported `requirements.txt` files provide the best coverage.

Eligible installations are cloned into a private prefix. A private PATH launcher
exposes them to the image's exact default `python3` and installed console scripts;
`python3 -m pip install --require-hashes -r requirements.txt` then sees satisfied
requirements. Other Python versions, explicit virtualenv executables and native
`pip` commands keep their normal behavior. A changed candidate requirement disables
this optimization. `.pth` distributions, sdists, direct references and unsupported
setups fall back to normal installation. Unsupported installer outcomes are
remembered for at most five minutes; timeouts/OOM are not cached as installations. Unsupported formats
(such as Yarn and pnpm), unpinned dependencies, custom indexes, arbitrary URLs,
Git/local sources and generated artifacts use existing package-manager behavior.
They are reported as unsupported, rather than counted as hits. Cache availability
does not grant additional sandbox network access. Deleting a private run directory
waits for successful container removal; a failed removal leaves it quarantined.

Diagnostics, agent results and evaluation execution receipts include
`download_cache`: hits, misses, successful acquisition counts, availability, exact
versions, integrity algorithms/hashes, manifest paths, registry and hashed source
identity. A hit means an artifact was staged; it does not prove a command used it.
Raw source URLs and download exceptions are omitted from receipts to avoid secrets.
These records are included before existing artifact receipts are sealed.

The archive store, staging seeds and wheel environments each have a 2 GiB
maintenance budget. Public index URL metadata has a separate 128 MiB cap.
Verified materialized starting states have a separate 2 GiB budget. Maintenance
skips entries currently being acquired, built or cloned; active use can temporarily
exceed a budget. Status reports derived-cache sizes separately.
Private copies remain usable during pruning. Inspect or prune both this store and
the legacy npm store with:

```sh
mineval --data DATA cache status
mineval --data DATA cache prune --max-mb 512
mineval --data DATA cache prune --max-mb 0
```

A zero budget also clears public PyPI URL metadata and idle materialized starting states. Active/quarantined task copies
are not treated as shared entries and are never deleted by pruning.

## Reusable settings

Optional `DATA/historical-settings.json` is loaded by default. `--settings FILE`
selects another trusted host JSON file. Example:

```json
{
  "preparation_image": "mineval-preparation:local",
  "preparation_model": "gpt-6-luna",
  "preparation_effort": "medium",
  "review_model": "gpt-6-luna",
  "review_image": "mineval-review:local",
  "evaluation_model": "gpt-6-luna",
  "evaluation_effort": "medium",
  "architecture_review": true,
  "concurrency": {"total": 4, "prepare": 1, "benchmark": 2, "evaluation": 1, "review": 1, "per_model": 2},
  "models": [
    {"model": "gpt-6-luna", "effort": "medium", "adapter": "codex"},
    {"model": "gpt-6-luna", "effort": "high", "adapter": "codex"}
  ],
  "evaluation_configs": {"TASK_ID": "private-checks.json"}
}
```

`evaluation_config` may name a common default private-check command configuration;
`evaluation_configs` overrides it per task. Paths resolve relative to the settings
file. Settings contain configuration, never credentials. Supply authentication only
through the explicit launcher flag. `--image` overrides the default preparation image
for this server/worker. `review_image` selects the trusted Codex-capable image for
qualitative review and compaction. It defaults to `preparation_image`, then the saved
benchmark's configured agent image, independently of the private test image. Review
failures are labeled separately and preserve benchmark and evaluation results;
retrying them reuses both saved artifacts. Set `architecture_review: true` to queue architecture records and comparisons, or `false` to disable them. Set `qualitative_review: false` to disable the separate legacy qualitative memory review. Separate sessions and frozen independent review are always required to
prepare tasks, regardless of these evaluation/review options.

The extractor is asked to supply a structured `evaluation_config` in its private
plan, including a tests command, known runtime, private paths, exit-code semantics
and limitations. This is validated, shown to the independent reviewer and frozen
with verification. Old plans remain readable. For a legacy plan without runnable
commands, configure them once in settings. A missing config blocks that selection
with actionable guidance before spending a benchmark call. Mineval does not interpret
free-text extraction instructions as a shell command or guess arbitrary test semantics.
Commands execute only inside disposable rootless sandboxes. Queuing snapshots the
validated config; later settings edits do not change that queued evaluation.

Automatic evaluation runs fixed private cases without an evaluator model. The isolated-argv-v1 runner keeps case files outside the candidate container and compares
captured output on the host. isolated-http-v1 starts a service in a network-isolated
candidate container. A transport inside that container sends one HTTP request at a time
to localhost; the trusted host compares status, headers, exact response bodies or JSON
against private expectations. Ordered steps support stateful behavior and cleanup
requests. The candidate can observe requests already sent and can control its own HTTP
responses, but the private test documents, unsent requests and expected values are not
mounted or sent into its environment. The HTTP service and transport require Python 3
in the selected image. The service must expose a stable HTTP interface; arbitrary
in-process Python tests, direct imports, database access, browser execution, and other
test types are not supported by this boundary. Older shared-environment configurations
fail closed. New freezes require reviewed validation receipts showing a failing base
and, when a reference exists, a passing reference with the same fixed configuration.
Older revisions without a pinned configuration retain their original evidence and still
require an operator-selected configuration.

Architecture review runs in the existing review phase after functional evaluation and
cannot change its verdict. Records and comparison history appear on the task and run
pages. Comparisons receive anonymous labels and source copies; candidate source may
itself contain identifying information, so anonymity cannot be guaranteed against
such clues. Only successfully recorded attempts are included. Each comparison is saved
separately, and later completions trigger a fresh comparison across available records.

## Queue, exclusion and interruption

`historical-jobs.sqlite3` stores the queue and execution phases, with WAL and full
synchronous commits. One coordinator owns crash recovery and schedules bounded
stage workers. Benchmark execution, evaluation, and qualitative review
have separate configurable limits and return to the queue between phases. Inference
does not hold a database transaction. Multiple models can run the same prepared task
concurrently; per-run ownership excludes duplicate launches and unsafe recovery.
Task mutation remains exclusive, while immutable run/evaluation readers share task
ownership. Failed tasks do not stop unrelated tasks.

`concurrency` accepts positive integers from 1 through 64. Defaults are preparation
1, benchmark 2, evaluation 1, review 1, per-model 2, and total `min(4, max(1, CPU
count // 2))`. Total bounds all active stages; per-model also reserves the configured
independent preparation reviewer. Set these limits to fit container memory/CPU
budgets and provider quotas. Preparation sessions retain their own bounded worker
slots. The low-level explicit suite runner remains sequential.
The UI's **Activity** page shows queued/running/failed/interrupted/completed state, current
stage and links to saved run and assessment IDs. Refresh to update. A server polls
for queued work; a standalone CLI worker drains the queue once.

Active work has a unique key of operation, task, complete frozen revision and
model/effort/adapter identity. Repeated selection returns the active job. Browser
forms also have persistent, single-use request IDs, protecting reload/double submits,
even when rerun is selected. Clear exclusion to deliberately allocate a new attempt.
Exclusion matches schema-2 runs with the **entire prepared revision**, model identifier,
reasoning effort and harness name/version. Even unsuccessful prior attempts count
as attempted; explicitly rerun to try again. Historical runs with unspecified effort
are not silently treated as medium. The low-level `bench run` command remains an
explicit new attempt and adds `--effort` for both dedicated adapters.

On worker restart, previously running jobs become interrupted and require an explicit
retry. Finished runs are reused for evaluation retries; published assessments are
reused when only model review failed. Uncertain/orphan evaluator evidence reserves its
ID: retries allocate a new assessment ID. A run left `running` by host loss is sealed
using existing `bench recover`, then the job reports that a new explicit rerun is
needed. Attempts without collected output cannot be retried toward evaluation.
Activity exposes “Start new benchmark attempt” for these jobs, allocating fresh run
and assessment IDs while preserving the old evidence; ordinary retries are rejected.
Partial evidence and immutable launches are never overwritten. Interrupted
assessments remain internal evidence after a replacement assessment completes;
they never become an earlier published score.

`TASK/preparation-progress.json` records completed cleanup, diagnostics, extraction,
base/future validation and approval checkpoints. Reuse checks exact current source,
draft, configuration, model/prompt, resolved runtime and sealed output receipts.
Cleanup changes bind their resulting draft. A producer's exact current diagnostic
is reusable. Failed/incomplete stages are not checkpoints; changing inputs reruns
affected stages and invalidates downstream work. Approval must still satisfy
`review.approved` immediately before freezing. An already frozen revision is checked
and reused.

Normal sandbox cleanup remains in force. A hard process kill/host loss can still
leave labeled Docker containers, as documented in the existing sandbox contract.
Closing the web server or interrupting the coordinator signals active sandboxes,
kills waiting workloads, waits for lifecycle cleanup and leaves unfinished jobs
interrupted. Work that has already sealed its output can resume from the next phase. A forced
kill while waiting can still leave labeled containers; inspect exact leftovers
before starting more work. The queue is durable, not a container supervisor across
host failure.

Equivalent CLI workflows:

```sh
mineval --data DATA workflow --settings settings.json prepare-all
mineval --context ROOTLESS --data DATA workflow --settings settings.json --auth-file /explicit/auth.json work
mineval --data DATA workflow list
mineval --data DATA workflow retry JOB_ID
# For an interrupted/recovered attempt without saved output:
mineval --data DATA workflow retry JOB_ID --new-attempt
mineval --data DATA workflow submit selection.json
```

`selection.json`:

```json
{
  "kind": "benchmark",
  "selections": [{"task_id": "TASK_ID", "version": "EXACT_VERSION"}],
  "configurations": [{"model": "gpt-6-luna", "effort": "high", "adapter": "codex"}],
  "exclude": true
}
```

The existing individual cleanup/extraction/validation/review/finalize, benchmark,
export, recover, evaluation and explicit comparison commands remain supported.

## Categories and performance

The discovery skill gives the selecting model seven fixed engineering areas:
behavior, integration, data, reliability, security, developer-tools and user-interface,
with definitions and an instruction to choose one primary obligation. Intake validates
a required primary category on new `schema_version: 2` selector handoffs. Unversioned
and schema-1 handoffs retain legacy category handling; older handoffs and stored records without one display as
uncategorized. Old change-type labels such as feature and bugfix remain importable
and display as uncategorized; arbitrary labels in existing records are retained
without pretending they belong to the new taxonomy. No historical metadata or receipt is rewritten or inferred from a title.

Workflow runs stay out of summaries and explicit comparisons until required
evaluation and qualitative review finish. Failed reviews keep their
benchmark/evaluation evidence for retry without publishing a final result. Raw
evidence and progress remain inspectable. Legacy and standalone CLI assessments
retain their existing publication behavior. Qualitative observations cannot change
a behavioral verdict.

Models and results shows each model/effort/harness configuration's all-evidence
performance and category average. Each dedicated model profile shows pass/fail/inconclusive/error,
pass rates, decided counts, denominators, task coverage, exact assessment selections,
and accumulated observations. Pass rate includes all four verdicts in the denominator.
A sample is an exact prepared revision and evaluation condition. Repeated reruns
use the first assessment of the earliest attempt for that sample; no best score is
selected. Distinct revisions/conditions remain distinct samples; task coverage counts
distinct task registrations, not samples, against prepared registrations including
archived history. Overall coverage uses the same distinct-task denominator. Separate
sample coverage divides evaluated samples by the union of observed exact revision/condition
samples across configurations (not unobserved possible conditions). The category average equally weights **observed named categories**;
missing categories are excluded and their number is shown. Uncategorized samples
contribute to overall rates but not the named-category mean. Zero data yields an
unavailable rate, not zero. Small sample counts are visible rather than ranked.

All-evidence summaries on different subsets are descriptive, not model rankings.
Comparable rates use only the intersection of exact revisions, verification receipts,
evaluation config hashes, policy and resolved image IDs across every listed
configuration. Missing resolved IDs do not match known images. Use Compare for
explicit pairwise assessment selections and its existing stricter compatibility checks.

## Model memory and safe compaction

`model-reviews.sqlite3` provides a logical model namespace, shared across efforts.
Each observation retains model/effort/harness, exact revision, run/evaluation IDs,
evaluation hash and verdict. Direct CLI evaluations append a conservative factual
observation too. `model-review record TASK EVALUATION` repairs a missing observation
idempotently after a host interruption. Detailed task evaluation remains authoritative.
Qualitative inference sees the saved independent evaluation, agent completion report,
selected-check evidence, task statement, bounded code diff and prior active model
observations; it may describe recurring behavior only when current evidence supports
it. Textual observations cannot change an independent verdict.

At 24 active observations the worker folds the oldest two into one summary until
the active list is below 24. A separate review model does this outside a transaction.
The summary takes their oldest position, unions source references and records parent
IDs. A failed inference releases its reservation immediately so retry need not wait
for expiration. Originals never disappear; The model profile exposes the full archive and CLI can
retrieve it. Reservations last 15 minutes by default, with a maximum of one hour.

The CLI is also a compaction protocol for independent review agents:

```sh
mineval --data DATA model-review history MODEL --archive
mineval --data DATA model-review claim MODEL --ttl 900
mineval --data DATA model-review inspect-claim RESERVATION_ID
mineval --data DATA model-review commit RESERVATION_ID summary-bullets.json
```

`claim` returns null below threshold or if the oldest pair is reserved. A successful
claim returns the reserved original contents and references. `commit` accepts a JSON
array of 1–8 concise bullets; only a live reservation can atomically deactivate those
two active inputs and publish its summary. Another agent cannot claim the same pair.
Appends continue during inference and are preserved. Expired claims can be reclaimed;
a stale holder cannot commit. Identical repeated commits return the existing result;
different content for a committed reservation is rejected. No global inference lock
is held. Observation rows and event provenance are append-only; only the active
projection and reservation lifecycle mutate. Use `model-review review` or `compact`
with an explicit trusted `--image` and optional model-only auth file to invoke the
same sandboxed review model outside the workflow.

## Security and validation limits

Historical observation still binds loopback, uses a fresh secret URL, validates Host,
escapes evidence, disables caching, forbids framing and external resources, and serves
artifact contents as escaped text. Only the standalone historical server enables
operations. POSTs additionally require exact same-origin Origin, a session CSRF token,
form content type, bounded input and single-use request IDs. Operating pages use
`Referrer-Policy: same-origin` so browser form POSTs retain a verifiable Origin;
private URLs are never sent to other origins as referrers. Browser callers select
tasks/models/efforts; they cannot supply Docker flags, host commands, credential paths
or runtime paths and commands. Model settings accept only model identifiers and efforts. Generated Arena sites retain their isolated origin and
cannot call these operations. Do not proxy/tunnel/share the private human URL or
place it in an agent environment. Queue error messages suppress raw infrastructure
exceptions and retain deliberate per-task recovery guidance.

Tests use real temporary Git history, actual immutable artifact publication, HTTP,
SQLite concurrency and simulated isolated agents. They do not prove that any chosen
image, installed Codex version, credentials, model identifier, effort setting, project
dependency or extraction command works in a live runtime. No paid model calls are
made by the tests. Rootless Docker cold/warm and batch measurements are documented
in [Incremental pipeline validation](incremental-pipeline.md); no live-model smoke
test was performed for this change. Existing
opt-in rootless runtime tests remain available.

### Independent memory maintenance

Required qualitative review queues a durable, deduplicated request per observed model.
Run `mineval workflow maintain` in a separate process or user timer. Publication
does not wait for compaction. Requests survive restarts; SQLite reservations serialize
the oldest inputs and expire after interruption. Failed inference leaves observations
active and does not require a benchmark rerun. Maintenance uses the invoking worker’s
authentication file; credentials are never stored in the queue.

Resource admission also checks available host/cgroup memory and CPU capacity. Configure
`resources` budgets as described in [installation](installation.md). Optional
`model_concurrency` maps individual model identifiers to provider limits; the general
`concurrency.per_model` remains the fallback. When no workflow jobs are running, one eligible preparation job is attempted even
under low capacity. Additional concurrent jobs that do not fit remain queued.

Evaluation command records include bounded resource diagnostics when the local
container cgroup is readable. `pressure_diagnostics` records sampled PSI `some
avg10` baseline, peak, end and memory counters for the container lifetime. Host,
worker ancestor, and container PSI are telemetry only; PSI does not affect
admission or verdicts. `ancestor_0` is the Mineval process's cgroup and subsequent
indices walk toward the root. Missing readings are null; an unavailable cgroup
cannot establish that pressure was absent. Per-command `pressure_stall_us` gives
changes in PSI cumulative stall microseconds for each scope; PSI avg10 is a rolling
estimate and can remain elevated after stalls subside. PSI is evidence of pressure,
not proof that it caused a particular failure. New evaluations use a distinct
policy identifier so comparisons do not conflate them with PSI-gated assessments.
Command classification, limits and
diagnostics also appear in the private `dev.jsonl` log as
`evaluation.command.classified`. Docker limits, OOM detection and timeouts remain
in force.

### Isolated HTTP grading format

Use a trusted service startup command and a final `tests` marker in the frozen
configuration. The marker's argv is not executed; the private case documents drive
the requests. For example:

```json
{"image":"local:python", "network":"none", "grading_type":"isolated-http-v1",
 "service":{"argv":["python3","-m","uvicorn","app:app","--host","127.0.0.1","--port","8000"],"port":8000},
 "commands":[{"name":"http-cases","kind":"tests","argv":["true"]}]}
```

Each selected or generated document has named, nonempty cases and ordered steps:

```json
{"cases":[{"name":"create then read","steps":[
  {"method":"POST","path":"/items","headers":{"Content-Type":"application/json"},"body":"{\"name\":\"pen\"}","expect":{"status":201}},
  {"method":"GET","path":"/items","expect":{"status":200,"json":[{"name":"pen"}]}}
]}]}
```

Assertions use exact status and optional exact body, JSON value, or header values.
Each step executes even after an assertion failure, allowing explicit cleanup steps.
The candidate container is discarded on completion or infrastructure failure. All
case and step counts are checked before a pass is published. Requests already sent
are visible to the application, so private inputs are not secret once exercised.


## Repeated attempts and comparison

The default is one attempt per revision and model. Use `mineval bench run --attempts N`
for an explicit task or suite run, or set `attempts` in the workflow submission JSON.
The benchmark picker offers the same setting. To run a new batch for a model and
revision that already have an attempt, clear **Skip already benchmarked tasks** or
set `exclude: false` in the workflow submission. Each attempt has its own run ID,
disposable workspace and fresh service state.

Comparisons match the exact prepared revision, grading rules and resolved environment.
Coding and grading budgets are reported separately by cohort. Pass rate is passed
divided by decided attempts. Reports also show the number of attempts, inconclusive
results, infrastructure errors and exclusions. A regrade reuses the original attempt
identity. Summary views use its first published assessment. Explicit comparisons
accept a chosen assessment per attempt.
