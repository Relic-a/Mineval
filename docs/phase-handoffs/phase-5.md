# Phase 5 — private verification, independent review and approved freezing

- **Implemented:** `verification-extract` and `review` run separate, fresh isolated
  sessions through explicit agent executables or the existing dedicated Codex adapter.
  Private disposable copies include base/cleaned/future trees, diffs and context;
  reviewers additionally receive cleanup, extraction and validation evidence. No
  source/store/home/socket mounts or agent transcripts. Extraction preserves checked
  original bytes, commit/path/purpose provenance, focused/fuller strategy, rationale,
  dependencies, running instructions and limitations. Unavailable evidence saves a
  needs-review attempt. `verification-check` optionally runs extracted checks against
  cleaned base or selected future state in fresh rootless containers, recording
  environment versus behavioral failures privately without changing cleanup diagnostics.
  Independent approve/revise/reject reports include findings and per-path reasons.
  `finalize --review-id` requires approval bound to exact current inputs and freezes
  private verification, validation and review alongside existing snapshot/environment
  receipts. Materialization exports only cleaned files and sanitized pre-base Git.
  Task inspection lists private extraction/review/attempt IDs. README, foundation and
  report examples document the commands and migration.
- **Decisions made during this phase:** Session independence means fresh host-assigned
  identities, containers and ephemeral agent state; the same underlying model may
  serve different roles. Semantic quality/leakage judgments remain reviewer obligations,
  not filename heuristics or claims of mechanical proof. Explicit fake/local agent
  executables use the same structured-report interface. No automatic pipeline or human
  approval gate. Legacy `extract` and `extraction-session` artifacts remain readable,
  but cannot satisfy new session-produced verification requirements. Inline legacy
  `finalize --review` attestations cannot freeze new revisions; old frozen revisions
  remain readable. Revise/reject applies to that preparation report; explicit task
  rejection remains separate. Validation is optional when infeasible; every added
  validation receipt invalidates prior approval. Future validation uses original future
  files without applying cleanup adaptations; setup limitations remain visible.
- **Checks performed:** Full suite with `MINEVAL_TEST_CONTEXT=mineval-rootless`,
  `MINEVAL_TEST_IMAGE=mineval-fixture:phase4`, and
  `MINEVAL_AGENT_TEST_IMAGE=mineval-fixture:phase4`: **72 passed, none skipped**.
  After adding an additional disposable-copy/source-byte tampering test and final
  refinements, all **7 phase-5 tests passed**, including live rootless extraction,
  reviewer execution, private base/future checks and freezing/materialization. The
  real Git fixture extracts a regression committed after the solution: it fails on
  the unsolved base and passes on the future state. Fake reviewer fixtures flag
  future production selected as test support and arbitrary verification assumptions;
  revise/reject cannot freeze. Checks cover inline/legacy bypass prevention, stale
  approval, artifact tampering, new immutable revisions, unavailable evidence,
  process/OOM failures and separation of private validation from cleanup diagnostics.
  All **10 registry tests** also passed after the inspection/CLI refinements.
  CLI help and `git diff --check` passed. No managed containers remained afterward.
  **No real Codex/model calls**; new role-specific Codex prompts remain untested live.
- **Remaining limitations/blockers:** Reviewer semantics and adequacy are not proven by
  fixture reports. Supporting services/dependencies still require explicit pinned
  configuration; no automatic provisioning or safe credential discovery. Future state
  checks may need setup adaptations and are evidence, not a rule that the historical
  patch is the only correct solution. Existing rootless bind-disk quota, open-network
  auth exposure and OS-owner artifact-tampering limitations remain. Failed private
  processes do not publish approval. No benchmark execution, grading or observation
  UI was added.
- **Inputs for next phase:** `review.session`, `review.validate`, `review.approved`,
  `Store.finalize(..., review_id=...)`; immutable extraction manifests and private
  review/validation receipts; frozen `private/verification`, `private/review`, optional
  `private/validations`, and `approval.json`. Benchmark agents must receive only
  `Store.materialize` output, never the version directory or private artifacts.
  Existing immutable revision/suite/run identity contracts remain intact. Stop here.
