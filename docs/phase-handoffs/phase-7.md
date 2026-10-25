# Phase 7 — independent, inspectable evaluation and re-evaluation

- **Implemented:** `mineval evaluate RUN --task TASK --config CONFIG` evaluates a saved
  candidate in fresh rootless containers against its exact frozen private verification.
  Original checks replace candidate files and receive read-only nested bind mounts.
  Optional explicit/Codex evaluator sessions inspect original execution results and
  propose evidence-cited interface adaptations; those execute in another fresh saved
  candidate copy. Structured provenance, original/executed hashes, outcomes, justified
  adaptations, limitations and explanations survive; no transcripts/session state.
  `evaluation show/compare` and `task evaluation` validate immutable schema-2 assessments.
  Re-evaluation allocates a new ID and per-run version, preserving every prior verdict.
  Pass/fail/inconclusive/error have CLI exits 0/1/2/125. README, foundation and evaluation
  configuration/comparison examples document usage and retention.
- **Decisions made during this phase:** Reuse task-local `evaluations/ID.json` with
  hash receipts; seal evidence under `evaluation-evidence/ID/` before publishing the
  record. Pin launch JSON, sealed output manifest, exact revision, verification,
  config, policy and evidence. Legacy reservations remain readable, without scores.
  Require explicit trusted verification commands; tests default to exit 1 for a
  behavioral failure, 2 for inconclusive, and other nonzero codes for error, with
  validated per-command outcome overrides. Wrappers must separate assertion failures
  from collection/dependency errors. Evaluators may explicitly explain execution
  errors or insufficient evidence, never promote infrastructure errors to pass/fail.
  One evaluator proposal and at most one adapted execution per assessment; no hidden
  retries or coding-model reruns. Compare explicit assessment IDs on exact common
  revisions, equal config/verification/policy and matching resolved runtime images
  where available. Retain error/inconclusive counts in the common-set denominator,
  with decided coverage; no common tasks means null rates. Existing saved results
  remain evaluable after task retirement/rejection.
- **Checks performed:** Full suite with `MINEVAL_TEST_CONTEXT=mineval-rootless`,
  `MINEVAL_TEST_IMAGE=mineval-fixture:phase4`, and
  `MINEVAL_AGENT_TEST_IMAGE=mineval-fixture:phase4`: **90 passed, none skipped**.
  Eight phase-7 tests cover a real pass/failure, legitimate alternate interface and
  incorrect alternate answer, inconclusive evidence, infrastructure/command/timeout
  errors, exit contracts, invalid provenance, immutable re-evaluation and restart/
  separate-process inspection, input/evidence tampering, exact comparison coverage,
  policy/runtime mismatch and CLI outcomes. The phase-7 suite was rerun after the
  final comparison guard; the full suite then passed again after correcting a
  mount-loop variable that had shadowed the container cleanup identity. A separate
  sandbox regression and live container-removal assertion cover that bug; the four
  affected development containers were removed. Live rootless verification confirms private checks resist
  rewriting/deletion while frozen revisions and original run outputs stay unchanged.
  CLI help, example config validation and `git diff --check` passed; no managed
  containers remained. **No real Codex/model calls**; evaluator model quality and
  live Codex adaptation are untested. Simulated evaluator fixtures exercise the
  structured report boundary; only the private-check executor was tested live here.
- **Remaining limitations/blockers:** Command selection and adaptation semantics
  require trusted judgment/inspection; provenance validation does not prove behavioral
  equivalence. Read-only checks prevent file replacement, not adversarial runtime
  manipulation. Generic commands cannot reveal whether exit 1 represents a broken
  test harness; configure wrappers correctly or use evaluator error/inconclusive
  findings. Existing owner-tampering, unsupported links/submodules, bind-disk quotas,
  reproducible dependencies/services and code-embedded secrets limitations persist.
  SIGKILL can lose an in-flight assessment or leave orphan sealed evidence (use a new
  ID), and cannot guarantee cleanup. The observation UI remains phase 8.
- **Inputs for next phase:** `evaluation.evaluate/inspect/compare`,
  `Store.inspect_evaluation`; `evaluations/ID.json` plus `.sha256`, sealed
  `evaluation-evidence/ID/{config,report,assessment}.json`, original verification,
  `baseline/{selected-checks,execution}.json` and optional `adapted/` outcomes.
  `task show` lists assessment IDs; comparison returns exact coverage, explicit
  selections and common-set verdict counts/rates. Phase 7 stops here.
