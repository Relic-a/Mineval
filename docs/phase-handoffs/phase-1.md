# Phase 1 handoff — local foundation and durable identities

## Scope and starting point

The authoritative contract is [mineval-decisions.md](../mineval-decisions.md).
Implementation details live in [foundation.md](../foundation.md); command usage is in
[README.md](../../README.md). This handoff stops at phase one.

At starting commit `af312ea`, most requested behavior already existed: externally
supplied persistent task IDs, duplicate rejection, Git/raw candidate import, private
provenance, mutable drafts, diagnostics, review/finalize, immutable prepared versions,
readable inspection, rejection/retirement, exact ordered suite pins, independent
workspaces and explicit `bench run`/`evaluate` placeholders. The existing
`import`, `prepare`, `finalize`, `verify`, `extract`, `workspace`, `reject`, and generic
sandbox `run` paths were retained. Historical selector guidance was reviewed; it remains
external discovery guidance rather than a runtime component.

## Changes delivered

- Added immutable, task-local run/evaluation identity reservations to the existing
  `preparation.Store`. Run records pin a prepared revision and model plus harness
  metadata; evaluation records pin a run and its hash. Both are explicitly `reserved`.
  Execution placeholders still create no records and call no model or Docker service.
- Added `suite list`, `task run TASK RUN`, and `task evaluation TASK EVAL` inspection.
  Retired revisions remain inspectable while mutation and workspace restrictions remain.
- Added an explicit workspace format descriptor to new prepared revisions, retained
  legacy reads, and made materialization verify the entire revision inventory and reject
  unknown formats. No old revision, suite, or run reference is migrated in place.
- Updated storage/command documentation. The requested handoff template did not exist
  in this checkout or its tracked history; a concise template was added and followed here.

## Validation

Run `python -m unittest discover -s tests -v` from the repository root.
Baseline: 44 tests, 3 live Docker checks skipped. Final: 47 tests, 3 live Docker checks
skipped. Focused fixtures cover real temporary Git imports, duplicate import identity,
persistent reopen and independent CLI processes, immutable revisions and ordered suites,
pool growth and retirement, reserved run/evaluation reloads and tamper detection,
legacy format reads and unsupported-format rejection. Diagnostics use simulated runners.
No real coding models were called; no live Docker tests were requested or run.

## Compatibility and limitations

No architecture replacement or external dependencies. Existing stores remain readable;
new writers create missing record namespaces when necessary. Identity reservations are
available through the Python API only. Later execution/result artifacts must reference
these immutable identities rather than overwrite them. OS-owner tampering remains outside
the application immutability guarantee; hash receipts detect changes.

Exports still have no `.git`. The new descriptor provides a forward migration boundary,
not sanitized history itself. A later writer must publish new revisions with sanitized
pre-base history and a supported format, retaining old readers and exact historical pins.
Full source repositories and private future evidence must stay outside agent workspaces.

## Later phases

No phase 2–8 specifications or numbered plan were supplied in this checkout, so the
following are remaining integration areas across those phases, not invented phase numbers:

- Sanitized historical Git through the base, excluding future refs, objects, metadata
  and repository configuration; safety fixtures must exercise merges and unreachable data.
- Independent cleanup/reviewer orchestration and enforcement beyond current attestations.
- Dedicated harness adapters and benchmark execution with isolated writable workspaces,
  normal configurable web access, model/harness versions and configuration, and structured
  completion/failure outcomes attached to reserved identities.
- Versioned private verification inputs and evaluation policy/execution, separate from
  preparation diagnostics; evaluation results must pin their verifier inputs and run outputs.
- Result inspection/comparison using model plus harness identity, with no stored transcripts,
  budget tracking, public leaderboard, configuration-ranking system or centralized service.
- End-to-end validation of those later workflows while preserving existing suite and run
  references and keeping human inspection/retirement available.
