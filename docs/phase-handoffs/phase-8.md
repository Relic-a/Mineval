# Phase 8 — human observation and final MVP integration

- **Implemented:** `mineval observe [--port PORT]` serves a local, read-only human
  interface over real store data. Linked task/revision/suite/run/evaluation pages
  expose candidate provenance, preparation history, cleanup and diagnostic findings,
  independent review, private verification provenance/limitations, saved output diffs,
  verdicts, commands and adaptation evidence. Explicit comparison forms reuse the
  existing common-revision/policy checks and show common coverage and verdict counts;
  zero common tasks has unavailable rates. `task retire TASK --reason REASON` excludes
  future work while preserving historical records and permitting saved-output
  inspection/export/re-evaluation. Suite preflight checks every member's eligibility
  before launching any. README includes installation, discovery-skill usage,
  preparation/benchmark/evaluation/browsing and troubleshooting; foundation docs
  describe storage and the UI boundary. Historical Git policy remains authoritative;
  clarified remaining old harness prose without changing legacy tree-only codecs.
- **Decisions made during this phase:** Standard-library HTML server, no new runtime
  dependencies, JavaScript, external resources, execution endpoints or agent-session
  features. Bind only IPv4 loopback; use a secret per-launch URL, Host validation,
  no-store responses, escaped content and restrictive CSP. This is private human
  access to future evidence, not a shareable multi-user service. Artifact routes use
  explicit receipt-checked namespaces; source code is never executed while browsing.
  Output diffs unpack saved trees into disposable directories, omit binary/large-file
  contents and bound preview size. Retirement appends a receipt-bearing record with
  the prior state, then changes mutable status; old immutable artifacts stay intact.
  CLI-only retirement keeps the observation surface read-only. No restore/delete
  feature or automatic reruns/refresh were added.
- **Checks performed:** Full suite with `MINEVAL_TEST_CONTEXT=mineval-rootless`,
  `MINEVAL_TEST_IMAGE=mineval-fixture:phase4`, and
  `MINEVAL_AGENT_TEST_IMAGE=mineval-fixture:phase4`: **94 passed, none skipped**.
  Final focused observation tests after presentation and edge-case refinements:
  **3 passed**, with the separate opt-in live walkthrough skipped in that focused
  invocation (it passed in the full suite). New checks cover real linked artifacts,
  receipt tampering, explicit/zero-common comparisons, retirement persistence and
  re-evaluation, all-member eligibility before suite launch, private-URL/Host and
  read-only HTTP boundaries, escaped candidate code, and path traversal rejection.
  `python -m examples.phase8_demo --directory /tmp/mineval-phase8-demo --context
  mineval-rootless --image mineval-fixture:phase4` also passed independently. It uses
  actual past/base/future Git commits, discovery-style JSON and separate CLI processes:
  import → no-edit fake cleanup/broker verify → fresh verify → private extraction →
  base-failing/future-passing validation → distinct scripted reviewer approval →
  immutable freeze → isolated fake suite benchmark → independent evaluation → eight
  real HTTP inspection routes. Past log/blame work, future commit/checks are excluded,
  receipts survive reopen, and pool growth/retirement leave suites/results unchanged.
  The fake run intentionally remains unsolved and correctly evaluates **fail**.
  Browser inspection at 1365px and 390px verified task/run/comparison views and no
  document overflow; a final bounded pass confirmed findings precede collapsed raw
  metadata. The design detector, CLI help and `git diff --check` passed. No managed
  containers remained. **No real Codex/model calls in this phase.**
- **Remaining limitations/blockers:** Scripted extractor/reviewer sessions validate
  integration and session separation, not semantic agent judgment or model quality.
  This walkthrough does not validate live Codex preparation/benchmark/evaluation.
  Existing rootless bind-disk quotas, unsupported links/submodules, source-embedded
  secrets, credential/network trust and owner-tampering limitations remain. Browser
  views acquire task locks and may ask for refresh during CLI operations. Large
  artifact sets are not paginated; text/diff previews are bounded and binary archives
  use CLI export. Do not proxy/tunnel the private observation URL to agents or others.
  Retirement has no restore command; corrected tasks need a new explicit identity.
- **Inputs for future work:** `mineval/observation.py` (`Pages`, `make_server`, `serve`),
  `Store.retire`, `tests/test_observation.py`, and `examples/phase8_demo.py`.
  The demo retains a human-browsable store plus `summary.json` in its new directory.
  All eight revised phases are implemented within the agreed MVP scope. Phase 8
  stops here; no paid smoke test or additional platform work is implied.
