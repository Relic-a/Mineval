# Mineval Web Arena

Web Arena is a separate benchmark type for frontend design. Its workflow is **create
brief → choose models/efforts → generate → interact → rank → share**. Benchmarks,
generations, individual human judgments, and reviewed exports persist between launches.
The Arena workspace includes the historical coding observation pages; the old
`observe`, preparation, benchmark, and evaluation commands retain their behavior.

## Start the workspace

```sh
python -m mineval --context mineval-rootless --data mineval-data arena serve \
  --image YOUR_PRELOADED_WEB_IMAGE --auth-file /path/to/model-only/auth.json
```

Use a trusted, preloaded image containing Python3, `/bin/sh`, `sleep`, Node/npm (for
npm apps), and Codex (for real generations). The existing
[agent Dockerfile](../examples/Dockerfile.agent) is a starting point: explicitly
review/build it and choose the Codex version you want. Mineval never pulls an image
or installs a host runtime. No credential is required for the fixture harness.
Model identifiers and effort availability come from your installed Codex/account;
unsupported combinations create failed dated runs rather than falling back silently.
The server prints a private loopback URL. Keep it private; do not proxy or tunnel it.
Only one Arena server may own a data directory at a time.

The UI supports frozen prompts, reference text/links, uploaded assets (20 files,
20 MiB total), and an optional `.tar.gz` starter (32 MiB compressed, normal artifact
limits after unpacking). Archive contents must be rooted at the project files,
without `.git`, credentials, dependency directories, links, or special files.
Assets are copied into `arena-assets/` for every run. Link text is frozen, linked
remote content is not: upload reference files for exact reproducibility. Changing
inputs requires a new benchmark so old and new generations retain the same task.

Add model/effort combinations to a batch, or add one at a time. **Add rerun** adds
an explicit new dated generation of the same configuration. No generation is
replaced or silently retried. The UI reports queued/running/ready/error states and
refreshes changed run states while preserving open website interactions. Runs in a
batch execute sequentially. Server shutdown cancels the active container and retains
the failed attempt; unstarted queued runs continue on next launch. After a hard
interruption, stale running entries become errors, preserving their identities.

CLI operations are also available:

```sh
mineval --data mineval-data arena create brief.json
mineval --data mineval-data arena list
mineval --data mineval-data arena show BENCHMARK_ID
mineval --context mineval-rootless --data mineval-data arena run BENCHMARK_ID \
  --image YOUR_PRELOADED_WEB_IMAGE --adapter fixture --model fixture-studio --effort high
# Real calls are explicit:
mineval --context mineval-rootless --data mineval-data arena run BENCHMARK_ID \
  --image YOUR_PRELOADED_WEB_IMAGE --adapter codex --model MODEL --effort high \
  --auth-file /path/to/model-only/auth.json
```

A minimal `brief.json` has `title` and `prompt`. Optional `references` is text,
`private` defaults true, `assets` is an array of `{name, data}` (base64 bytes), and
`starter` is base64 archive bytes. Do not run CLI generation concurrently with the
Arena server on the same store.

## Runtime contract and preservation

Arena reuses `DockerSandbox`, resource enforcement, explicit credential provisioning,
and checked tree archives. It deliberately does **not** call historical task intake,
Git reconstruction, cleanup, freezing, or private verification. The Arena runner owns
its own harness lifecycle; `CodexAdapter.stage()` only constructs the existing
isolated invocation, with an Arena-only completion schema and a per-run
`model_reasoning_effort` setting added. Future
harnesses can implement staging within this lifecycle without inventing historical
Git tasks. The initial configured harnesses are Codex and a deterministic fixture.

The agent receives only the brief, references, starter, and assets in disposable
directories. Only the agent stage can receive the explicitly provided model credential
file. Dependencies/build and preview stages receive no credentials or private store.
Agent output is quiesced before host inspection. Git metadata is discarded; internal
dependency links are materialized (npm executable links become wrappers at their
original target paths). External links, nested directory links, cycles, and special
files are rejected. Ordinary archive limits are 512 MiB and 100,000 entries, including
built dependencies; large projects may need a smaller standalone production output.

Generated projects declare `arena-runtime.json`:

```json
{
  "build": [["npm", "ci"], ["npm", "run", "build"]],
  "start": ["npm", "run", "start", "--", "--hostname", "0.0.0.0", "--port", "3000"]
}
```

These are **argv arrays**, executed only in rootless containers. Use the project's
actual production start arguments. The server must listen on port 3000. Static
projects can omit the file: the built-in Python server serves `dist/` when present,
otherwise the project root, with SPA navigation fallback. SSR/Node/custom HTTP
applications use the same protocol; they are not reduced to static HTML. Build
commands can install locked dependencies over the container network. After building,
the runner checks startup and HTTP response in a **network-disabled** container.
Ready means the homepage responds below HTTP 500; it is not a usability/design or
functional correctness verdict.

Each successful generation stores the complete built website/dependency tree in a
checked immutable-by-API archive, exact configuration, creation/start/finish times,
resolved image ID, runtime commands, build outcomes, and prompt hash. Reopening uses
a disposable copy of the saved build and pinned local image ID: no regeneration,
dependency reinstall, or build is required. Retain that Docker image and back up
`arena/` together. Image pruning can make an otherwise valid old result unavailable.
Builds are preserved byte-for-byte; external package registries and arbitrary agent
builds are not guaranteed to be deterministic on a new generation.

Previews use individual origins, capability URLs, sandboxed interactive iframes, and
rootless containers with no network or published Docker ports. A bounded HTTP relay
uses `docker exec` to communicate over the container's loopback. It supports normal
HTTP routes, forms, JSON requests, typing, scrolling, CSS/JS animations, and fullscreen
sized expansion. Generated commands never run on the host. Browser CSP blocks remote
subresources and connections, frame embedding, workers/service workers, and external
form actions. Management APIs require the launch secret, exact Host/Origin, and a
custom request header; there is no permissive CORS. Historical pages remain read-only.
Preview capability cookies are HttpOnly; application server cookies are namespaced
per runtime and never forwarded to a different website.

## Human ranking

All judgments retain two exact generation IDs, preference (left/right/tie), timestamp,
and an optional private note. Duplicate unordered pairs are rejected transactionally.
Ties contribute half a win to each side. A regularized Bradley–Terry fit derives a
provisional run ordering; a Gaussian prior keeps sparse/disconnected results finite.
The displayed `±` is an approximate local curvature scale, **not a calibrated
confidence interval**. Cycles and inconsistent preferences remain in the evidence.
Exact equal estimates share a position; unjudged results are unplaced. Overlapping
uncertainty remains visible instead of being presented as certain superiority.

Suggested comparisons prioritize a new result against established anchors, then
nearby uncertain positions, skipping already-judged pairs and well-separated
established pairs. A recommendation budget of approximately `n log₂(n)` judgments
(once every run has two judgments) bounds large tie clusters. Returning with a new
run opens new placement comparisons. This is a pragmatic incremental heuristic,
not a claim of optimal sample efficiency or a statistically complete tournament.

The leaderboard can show all dated runs, average every run within a model/effort
configuration, or collapse efforts by model. Individual results are always accessible.
No latest/best-run selection is performed. Anonymous viewing assigns stable model
aliases within a benchmark; repeated efforts/runs retain their same model alias.

Across-benchmark comparison uses an explicit selected task set and exact common
model/configuration identities. It compares observed preference against the **same
common opponents** with equal task/opponent weight. It does not average ratings
fitted against different opponent pools. Missing task/opponent judgments, or an
unjudged dated run, suppress that identity's overall score and expose coverage.
These descriptive preference summaries are not population model rankings. Benchmark
selection and human judgments can still be biased; the exact coverage stays visible.

## PNG and animated GIF sharing

Install the optional capture dependencies and browser in the environment that runs
Mineval:

```sh
python -m pip install -e '.[arena-share]'
python -m playwright install chromium
```

Alternatively set `MINEVAL_ARENA_CHROMIUM=/usr/bin/chromium` to use a trusted
installed Chromium; browser sandboxing remains enabled.

The Share tab selects up to four real website captures, PNG or animated GIF, model
anonymity, and whether to include title/brief excerpt. Private tasks default to no
title or brief. Two explicit privacy acknowledgments are required **before capture**.
The UI then displays the composed export for final inspection and download. Nothing
is uploaded or published automatically. Exports retain their exact selections,
privacy choices, timestamp, and file hash in the Arena store. The image includes
positions, evidence counts, selected/total output coverage, date, and a provisional
ranking label. GIFs sample three live frames, with a subtle progress accent even if
the website is static. Anonymous labels cannot remove identity clues or private
content embedded in the generated website itself.

## Validation and known limits

```sh
python -m unittest discover -s tests -q
MINEVAL_ARENA_TEST_IMAGE=mineval-fixture:phase4 \
  python -m unittest discover -s tests -p test_arena.py -q
python -m examples.web_arena_demo --directory /tmp/new-arena-demo \
  --context mineval-rootless --image mineval-fixture:phase4
```

Tests cover persistent reruns, input/output tampering, atomic batches, ties/cycles,
duplicate judgment avoidance, configuration grouping, conservative common coverage,
starter/path validation, npm executable preservation, runtime command validation,
privacy gates, and HTTP authorization. The opt-in fixture integration exercises
real rootless generation, dependency/build commands, dynamic HTTP/POST routes,
per-runtime cookies, offline startup, HTTP navigation, and unchanged archives
through close/reopen. Browser validation additionally exercises the complete UI,
interactive inputs, expansion, voting, grouping, and desktop/mobile layouts. No
real model calls are needed. Enable `MINEVAL_ARENA_CAPTURE_TESTS=1` under the optional
capture environment to verify actual PNG and three-frame GIF exports as well.

Current limits:

- Local single-user workspace; no accounts, cloud sharing, or public deployment.
- Production HTTP only: WebSockets/HMR, streaming/SSE, external live services,
  cross-origin resource dependencies, service workers, popups, and downloads are
  intentionally unsupported in previews. HTTP responses are buffered, capped at
  8 MiB; request bodies at 2 MiB. Applications should be self-contained.
- Cookies set by the application server work through namespacing. Client scripts
  that depend on exact `document.cookie` names may need adaptation. Local/session
  storage is naturally isolated by preview origin, but preview state is ephemeral.
- At most six active preview containers; opening another closes the oldest. Reopen
  any saved website to restore a fresh runtime. Very large apps can exceed artifact
  limits, and dependency directory links with nested links are unsupported.
- Preferences have one immutable judgment per pair. Undo/revision, multi-rater
  identity, calibrated statistical intervals, explicit rank-list input, and
  sophisticated active learning are future improvements.
- Reference links are passed as context; their remote contents are not archived.
- Capture needs the optional dependencies and a functioning sandboxed Chromium.
  GIFs sample the initial viewport, not recorded user interaction or full-page scroll.
- Historical observation and Arena have separate server commands. `arena serve`
  provides both surfaces through shared Mineval navigation.

### Implementation validation record

On the implementation host, the full standard-library suite passed (123 tests,
including optional live cases skipped by default). The opt-in rootless Arena suite
also passed with actual dynamic server build/POST/cookie isolation and actual
sandboxed Chromium PNG/three-frame GIF captures. The persistent demo reopened its
saved website without changing its archive. Desktop (1440px) and mobile (390px)
browser checks exercised creation, batch fixture generation, interaction/navigation,
expansion, ties, grouping, privacy review and PNG preview/download, with no page
errors or document overflow. Keyboard focus/title checks passed after navigation
and voting. An independent corrective UI review scored both requested fixes
(candidate A/B clarity and navigation focus) resolved. No real-model calls were made.
