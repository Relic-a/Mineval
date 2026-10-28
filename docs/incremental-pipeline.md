# Incremental historical pipeline

Mineval reuses verified preparation evidence and immutable starting states, schedules
independent workflow stages concurrently, and avoids AI evaluation when private
checks already give a decisive result. Frozen revisions, saved runs and assessments
remain independently inspectable. This applies to historical workflows; Web Arena
has its own execution path.

## Scheduling and ownership

One coordinator owns the durable SQLite queue and crash recovery. Workers execute
preparation, benchmarking, evaluation and review/compaction within separate stage,
total and per-model limits. Benchmark jobs return to the queue between stages, so a
slow review does not occupy a benchmark slot. Defaults are preparation 1, benchmark
2, evaluation 1, review 1, per-model 2 and total
`min(4, max(1, (os.cpu_count() or 2) // 2))`. Set `concurrency` in workflow settings
to integers from 1 through 64; see [historical workflows](historical-workflows.md).
These are admission limits, not automatic host memory or provider quota sizing.

Immutable task readers share ownership, allowing multiple models to benchmark the
same revision. Task mutation, each run identity and evaluation of a run retain
exclusive ownership. Assessment IDs also have exclusive ownership across runs;
version lookup ignores other runs whose record receipts may still be publishing.
Recovery cannot take over an active run. The explicit
low-level suite runner remains sequential.

Cancellation reaches waiting and executing containers. Workers retain lifecycle
ownership until cleanup ends; interrupted work requires an explicit retry. Each
launch still checks rootless Docker and resource-controller capabilities. Image
inspection is reused only by immutable image ID; mutable tags are resolved again.
A hard host/process failure can leave labeled containers requiring inspection.

## Preparation and materialization

`preparation-progress.json` checkpoints cleanup, diagnostics, private extraction,
base/future validation and independent approval. Keys bind the relevant workspace,
source evidence, configuration, image identity and agent/prompt settings. Reuse
checks current inputs and sealed output receipts. Failed or incomplete stages do
not replace successful checkpoints; rejection never becomes approval. A retry can
reuse a producer diagnostic or completed earlier stages when their inputs still
match, while changed inputs invalidate dependent work.

Materialized templates are keyed by the frozen artifact manifest. On first use,
Mineval unpacks the snapshot, reconstructs allowed Git history and records its
inventory. Reuse checks the template and creates a private clone. Files have
independent inodes: filesystem reflinks use copy-on-write where supported, with a
normal copying fallback. Agent workspaces never share writable template files.
The benchmark moves its disposable clone into the agent lifecycle instead of
copying it a second time. A verified history receipt replaces repeated full Git
validation on this internal path; external `agent-run` inputs still get full
validation.

Digest memoization uses device, inode, size, nanosecond mtime and ctime. Inventories
still check membership, file types and executable bits. Restoring an mtime does
not hide a content change. Reuse assumes host-owned evidence is quiescent; caches
are not a boundary against a hostile process running as the host owner.

## Dependencies and trust boundaries

`DependencyCache` is the shared dependency implementation. Per-artifact acquisition
locks deduplicate cold downloads without blocking unrelated artifacts. Receipt-
checked immutable staging seeds reuse manifest discovery, archive extraction and
source expansion, including Go module sources and Cargo vendor trees. Every
consumer gets private files. Generic npm lifecycle scripts and task-dependent native
compilation still run per task. A staged artifact hit does not prove that a package
manager used it, or that installation/build work was eliminated.

An additional pip optimization accepts only restricted pinned SHA-256 requirements
and bounded local includes. A separate offline, credential-free container runs
native pip with `--only-binary=:all:`, `--require-hashes`, `--no-index` and
`--no-deps`. It receives requirements and authorized wheel files, without project
source, active agent state, direct references or source build hooks. Native pip
validates markers, Python compatibility and wheel tags. The environment key includes
the immutable image ID and staged inputs. Changed candidate requirements disable
reuse.

Successful environments are verified and cloned into a private prefix. PATH
launchers scope imports to the image's exact default `python3` and installed console
scripts. Other interpreters, explicit virtualenv executables and native `pip`
commands retain their normal setup. Codex preserves the private PATH across its
clean environment; global `PYTHONPATH` forwarding is unnecessary. `.pth` packages
are not advertised as complete cached installations. Unsupported dependency forms
and other package managers retain native installation behavior.

Unsupported installer outcomes have a five-minute negative cache. Timeout/OOM
outcomes, including the interpreter probe, never publish installed environments or
negative entries. Agent-written files never flow back to shared caches. Cleanup
failure quarantines installer and run mounts rather than deleting files a surviving
container might still use. Inspect the labeled container and its mounts before
manual cleanup.

Raw objects, staging seeds, installed environments and materialized starting states
have separate 2 GiB maintenance budgets; index metadata has a 128 MiB cap. Pruning
uses the same keyed locks as acquisition/build/clone operations, skips active
entries and leaves private copies usable. Active entries may temporarily exceed
budgets. Same-template and same-seed clones currently serialize under their keyed
locks. Inspect with `mineval --data DATA cache status`; prune with
`mineval --data DATA cache prune --max-mb 512` (the limit applies separately to
cache classes, not as a combined store cap).

## Evaluation and publication

Passing private checks skip AI evaluation. Infrastructure failures, unknown exit
codes and missing saved output remain errors and also skip AI. Declared behavioral
failures or inconclusive results may invoke focused adaptation review. The evaluator
has configurable effort and a default 180-second deadline; private command limits
remain separate. Adaptations must preserve the behavioral assertions and are
executed independently.

Workflow assessments stay out of aggregate summaries and comparisons until required
review completes. Saved evidence remains inspectable while review is pending or
failed. Retrying an interrupted evaluation assigns a new immutable assessment ID;
the abandoned assessment remains internal after completion. Explicit standalone
assessments and legacy records have no workflow publication barrier. Qualitative
inference is deduplicated per assessment, and failed compaction releases its
reservation for retry.

## Fixture measurements and reproduction

The local rootless Docker fixture measured these elapsed times:

| Operation | Cold / serial | Warm / bounded |
| --- | ---: | ---: |
| Four fake benchmark runs plus four real private evaluations | 7.4848 s | 4.8128 s |
| Starting-state materialization | 0.0438 s | 0.0068 s |
| Pip installed environment build / reuse | 3.4649 s | 0.0033 s |
| Pip staging seed | 0.0130 s | 0.0022 s |

The bounded batch was approximately 36% shorter in this small synthetic fixture.
Native `python3 -m pip` reported requirements already satisfied; poisoning one
task-private environment did not affect the next environment. Installation figures
exclude the subsequent sandbox check (2.4261 s cold and 2.7268 s warm). An earlier
run measured 7.4483 s serial and 4.9758 s bounded (approximately 33% shorter). These are
individual local measurements, not production throughput, statistical benchmarks or
live model validation. No paid model calls were made.

Run from the checkout with a preloaded trusted image containing Python 3, pip and
Git, using a local rootless Docker context:

```sh
python tests/measure_incremental.py \
  --context mineval-rootless --image mineval-fixture:phase4
```

The harness uses an offline synthetic wheel, mocked host download responses, actual
Git/artifacts, fake coding agents and real Docker private evaluations. It never
pulls/builds images or calls a model. It prints JSON and asserts correctness without
asserting speed thresholds. Filesystem support, image contents, container startup,
host load, task size and model latency all affect the results.

For correctness checks:

```sh
python -m compileall -q mineval
python -m unittest discover -s tests -q
git diff --check
```
