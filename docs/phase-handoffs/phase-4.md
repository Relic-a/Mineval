# Phase 4 — minimal cleanup and fresh-environment diagnostics

- **Implemented:** `mineval prepare TASK -- EXECUTABLE ...` now mounts a disposable
  draft/control copy with freshly reconstructed, hashed pre-base Git history. New
  `mineval cleanup TASK --adapter fake|codex` connects the Phase 3 adapters to the same
  workflow (fake default). Read-only task-preservation instructions forbid solving
  the task and require per-path reasons and environment prerequisites. The existing
  task-scoped broker pauses the cleaner while copying its current workspace/config,
  then runs checks in a new sandbox. No source project, private evidence, host Docker
  API or host home is mounted. Agent logs are disabled. Agent Git is ignored on
  output; diagnostics rebuild trusted history instead of trusting agent metadata.
  `mineval verify` distinguishes environment failures from behavior failures and
  optional explicitly annotated expected task failures. Setup/build failures, missing
  commands, timeout/OOM and control failures stop later commands. Failing behavioral
  tests remain valid diagnostics. README, foundation docs, preparation Dockerfile
  and `examples/cleanup-report.json` describe the workflow.
- **Decisions made during this phase:** Keep draft trees Git-free; attach the protected
  history pack only to isolated session/diagnostic copies. Legacy raw exports remain
  supported by explicit commands without invented history. Each cleanup attempt gets
  an append-only ID under `tasks/TASK/cleanups/ID/`, storing `workspace.tar.gz`, tree
  hashes, binary patch against the original base, config, producer/input/prompt
  identities, diagnostic IDs and structured report. Only validated tree/config are
  copied back to the draft; unsupported filesystem output leaves the draft intact.
  Publication uses the store filesystem for renames. Missing/malformed reports,
  process failures and stale/environment-failed diagnostics require review. Producer
  dispositions are `ready-for-review`, `needs-review`, or `reject`, separate from task
  rejection and independent approval. Safe partial edits can be resumed. A tests-only
  `expected_failure` reason is an assertion for review, never automatic proof. Cleanup
  always uses bridge networking; diagnostic networking remains configurable.
- **Checks performed:** Full suite with `MINEVAL_TEST_CONTEXT=mineval-rootless`,
  `MINEVAL_TEST_IMAGE=mineval-fixture:phase4`, and
  `MINEVAL_AGENT_TEST_IMAGE=mineval-fixture:phase4`: **66 tests passed, none skipped**.
  After the final publication-path adjustment, all **6 focused cleanup tests passed**,
  including both live cleanup cases. The real Git fixture begins with an unavailable
  runtime prerequisite and an unfixed answer bug. The live cleaner invokes the broker,
  observes environment failure with skipped tests, edits only `runtime.txt`, then
  observes a different fresh container passing setup and failing the expected task
  test. Source history is unchanged; the future solution commit is absent from the
  cleaner and materialized cleaned revision. Checks cover discarded agent Git config,
  current/stale finalization boundaries, missing change reasons, process failure,
  explicit rejection, saved artifacts and dedicated fake-adapter invocation. The first
  live attempt exposed missing Git in the old shell-only fixture image; the corrected
  local image derives from `mineval-agent:phase3-ready` plus Mineval, with no network
  downloads. `git diff --check` passed. No managed containers remained after testing.
  **No real Codex/model calls** were made; live Codex cleanup remains untested.
- **Remaining limitations/blockers:** Task preservation, meaningful tests and adequate
  dependency/service pinning are semantic producer claims until Phase 5 independent
  review. No automatic provisioning of supporting services, credentials or project
  runtime dependencies. Existing rootless bind-mount disk-quota and open-network auth
  limitations still apply. Interrupted sessions preserve the prior draft; output
  collection failures do not publish unsupported trees. The old explicit
  `finalize --review` attestation API remains for compatibility; distinct cleaner,
  extractor and reviewer enforcement is not claimed in Phase 4. No benchmark or
  evaluation orchestration was added.
- **Inputs for next phase:** `cleanup.INSTRUCTIONS`, `validate_report`, and
  `save_attempt`; `service.session(..., adapter=..., auth_file=...)`; broker-bound
  `diagnose(..., workspace=..., config_path=...)`; attempt artifact hashes and
  diagnostic reports. `task show TASK` exposes `cleanup-latest.json`. If attempts
  exist, `Store.finalize` requires a matching `ready-for-review` attempt and copies
  it to the frozen version as `cleanup-attempt.json`. Phase 5 must separately produce
  private verification and independent review before permitting approved freezing.
