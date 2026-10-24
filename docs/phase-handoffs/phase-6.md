# Phase 6 — benchmark execution, immutable attempts and comparable suites

- **Implemented:** `mineval bench run --task TASK --version VERSION` or `--suite SUITE`
  connects frozen revisions to the existing isolated fake/Codex adapters. Disposable
  historical workspaces receive only the task statement, permitted pre-base Git and
  the web/solution-lookup trust policy. Each attempt gets immutable schema-2 launch
  identity plus separately sealed, hashed terminal status and safe final tree archive.
  Model + harness, exact revision, optional suite hash, instruction hash, image,
  resource settings and small self-reported completion are recorded; no transcripts.
  `bench list/show/export/compare/recover` provide inspection, tree export, exact
  common-revision coverage and explicit recovery after unclean host termination.
  Existing reserved identities remain readable. New freezes include hashed
  `task-instructions.json`. README, foundation and `examples/comparison.json` document
  usage, retention, statuses and migration.
- **Decisions made during this phase:** Reuse task-local `runs/ID.json` and its receipt
  without overwriting it; publish terminal artifacts atomically under
  `run-outputs/ID/`. Inspection joins both after receipt validation. Suite execution
  is sequential, assigning one independent ID per member; cancellation stops the
  sequence, with no automatic retries. Frozen diagnostic image digests take precedence
  over mutable image tags; older non-digest receipts retain their configured image
  reference and actual resolved ID. Existing revisions without frozen statements use
  the registered statement, copied and hashed into each launch identity. New attempts
  require sanitized historical Git. Comparisons select one attempt per task and one
  model + harness per side, reporting exact common revisions/coverage without scores.
  Successful harness completion is never a task verdict; malformed completion,
  process crash, timeout, cancellation and infrastructure errors remain distinguishable.
- **Checks performed:** Full suite with `MINEVAL_TEST_CONTEXT=mineval-rootless`,
  `MINEVAL_TEST_IMAGE=mineval-fixture:phase4`, and
  `MINEVAL_AGENT_TEST_IMAGE=mineval-fixture:phase4`: **81 passed, none skipped**.
  Eight phase-6 tests cover restart/independent-process inspection, two independent
  outputs leaving the entire frozen revision unchanged, mount/prompt isolation from
  private/future evidence, self-report retention, crash/timeout/cancellation, invalid
  completion and unsafe-output errors, immutable IDs, tampering, export protection,
  fixed suites after pool growth, partial/zero common coverage and stopping a suite
  after cancellation. Live rootless fake runs verify final exports and cancellation
  while a container is still running, preserving partial work only after removal.
  CLI help and `git diff --check` passed; no managed containers remained.
  **No real Codex/model calls**; the phase-6 Codex path reuses the previously tested
  adapter but was not smoke-tested with a model in this phase.
- **Remaining limitations/blockers:** Evaluation is phase 7; no pass/fail verdicts or
  aggregate scores exist. Legacy evaluation reservations pin only the launch identity;
  real evaluation must additionally pin the sealed output manifest. SIGKILL/host loss
  cannot guarantee container/staging cleanup or preserve uncollected output; explicit
  recovery records an infrastructure error and does not rerun or reclaim resources.
  The existing OS-owner tampering, unsupported links/submodules, bind-disk quota,
  credential visibility and reproducible service/dependency limitations remain.
  An agent can write secrets into its saved code; inspect exports before sharing.
- **Inputs for next phase:** `benchmark.run/run_suite/inspect_attempt/export/compare`,
  `Store.inspect_run`, immutable `runs/ID.json` and `run-outputs/ID/artifact-hashes.json`.
  Successful safe collections include `agent/workspace.tar.gz` and `agent/result.json`;
  terminal errors can have no workspace. Evaluate saved trees in new isolated copies
  with private verification from the exact frozen revision. Never mount revision
  directories/private evidence for benchmark agents. Phase 6 stops here.
