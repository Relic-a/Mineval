# Historical benchmarking from the web and CLI

Run `mineval --context YOUR_ROOTLESS_CONTEXT --data DATA observe --image YOUR_PRELOADED_PREPARATION_IMAGE --auth-file /explicit/model-only/auth.json`.
The printed secret loopback URL opens the historical task pool. The image must
already exist and contain Git, Python, Mineval and the isolated Codex executable
(and the dependencies the project needs). No images are pulled. The browser never
receives the auth path, credentials, host command execution or Docker APIs.
Web Arena continues to use its separate server, store, execution path and policies;
its embedded historical pages remain read-only. Use `observe` for historical actions.

The task pool lists every repository and supports repository, category, state,
prepared/unprepared and text filters. Check task rows to prepare, run or retire them.
Each prepared row lets you select an exact frozen revision (the current frozen
revision is selected initially). Prepare all considers eligible unprepared tasks
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
separate qualitative review and compaction when needed. The fake adapter is available
through settings/CLI for fixtures; its reviews are factual only. It never calls a model.

## Model settings and Codex discovery

Open **Settings** in the historical dashboard to select the **Prep / extract** and
**Evaluation** model and reasoning effort. Prep applies to cleanup, extraction and
independent preparation review. Evaluation applies to the evaluator, qualitative
review and memory compaction. The trusted host `review_model` / `review_effort`
settings can override the review sessions, and are called out on the settings page.

Choices are saved atomically in `DATA/historical-model-settings.json` and override
the corresponding host defaults for web and CLI workflow queues. Delete that file
to return to host defaults. New jobs snapshot these choices; changing settings
leaves existing queued jobs and their retries unchanged. Legacy jobs without a
snapshot resolve the current choices when executing.

Following Aproekt, Mineval starts the host `codex app-server --stdio`, performs
`initialize` / `initialized`, and pages through `model/list`. Discovery performs no
inference. It lists visible models, their supported reasoning efforts and defaults;
the task pool uses these models when no host `models` presets are configured.
Results are cached for five minutes (failures for fifteen seconds). **Refresh Codex
models** bypasses the cache. Explicit identifiers remain usable on discovery failure
or for models outside the visible catalog.

Discovery uses an isolated temporary Codex home, copying only the explicit
`--auth-file`, or the local Codex credential file when no explicit file is given.
It does not load host rules, plugins or configuration. Discovery requires a host
Codex CLI; execution still requires Codex in the trusted preloaded Docker image
and the explicit `--auth-file` when credentials are needed. The host catalog does
not prove that an older image runtime supports every discovered model or effort.

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
  "evaluation_effort": "high",
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
retrying them reuses both saved artifacts. Use `evaluator_agent: false` for checks-only evaluation or
`qualitative_review: false` to retain factual observations without qualitative model
inference. Separate sessions and frozen independent review are always required to
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

## Queue, exclusion and interruption

`historical-jobs.sqlite3` stores the queue and stage checkpoints, with WAL and full
synchronous commits. One worker holds a local filesystem execution lock; inference
does not hold a database transaction. Failed tasks do not stop unrelated tasks.
The UI's Queue page shows queued/running/failed/interrupted/completed state, current
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
The queue exposes “Start new benchmark attempt” for these jobs, allocating fresh run
and assessment IDs while preserving the old evidence; ordinary retries are rejected.
Partial evidence and immutable launches are never overwritten. Preparation
retries use fresh sessions/approvals; an already frozen revision is checked and reused.

Normal sandbox cleanup remains in force. A hard process kill/host loss can still
leave labeled Docker containers, as documented in the existing sandbox contract.
Closing the web server normally waits for its current bounded sandbox stage to
finish and clean up, then leaves the job interrupted before the next stage. A forced
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

Models and results shows each model/effort/harness configuration's all-evidence
performance and category average. More details shows pass/fail/inconclusive/error,
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
IDs. Originals never disappear; More details exposes the full archive and CLI can
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
made by the tests; no live-model smoke test was performed for this change. Existing
opt-in rootless runtime tests remain available.
