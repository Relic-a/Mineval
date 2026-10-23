# Mineval — agreed design contract (v2)

**Status:** Implementation baseline. This document records decisions agreed before implementation; it is **not** a claim that the current repository already implements them. Existing code and older docs must be inspected, but where an older spec conflicts with this contract, **this contract takes precedence**. Make the smallest safe migration instead of blindly rewriting working code. Significant changes to agreed decisions require human review; minor implementation choices may be documented in the phase handoff.

## 1. Goal and scope

Mineval is a **local-first, human-observable benchmarking tool** for coding agents. It builds reproducible tasks from real historical software-development changes, runs a selected model through a supported coding-agent harness against the unsolved historical state, and assesses the resulting work using private evidence from subsequent history. Its motivating use is **personalized benchmarks from one's own projects**, not a centralized public leaderboard.

Aim for a useful end-to-end tool, not an autonomous benchmarking platform. The human initiates operations, observes tasks and results, and may retire unsuitable tasks. Avoid agent-session management UIs, interactive evaluator steering, transcript replay, distributed workers, elaborate scoring/leaderboards, complex token/cost budgeting and broad plugin frameworks in v1.

## 2. Independent operations and responsibilities

- **Discovery (outside Mineval core):** Any user-chosen coding agent can read a portable Markdown **Moment Selector** skill inside any Git project and select good historical task moments. The **agent** makes the selections, guided by the skill. Discovery does not need to be part of Mineval's internal runtime.
- **`mineval task add`:** CLI validates, packages/copies and registers candidates. Do not expect discovery agents to manually copy artifacts into internal storage or know Mineval's directory layout.
- **`mineval prepare`:** Works on registered candidates. Includes a minimally cleaned historical workspace, private verification extraction and independent review. These activities can be invoked separately or resumed; there is **no forced continuous pipeline**.
- **`mineval bench run`:** Runs an agent against a **frozen prepared revision**. This operation never exposes future code, extracted private verification, or reviewer material to the benchmarked agent.
- **`mineval evaluate`:** Evaluates saved run outputs independently; can reevaluate results without running the coding agent again. Save clear assessment evidence.
- **Inspection/maintenance:** CLI and an uncomplicated local UI show tasks, versions, preparations, runs, evaluations and comparisons. Support retiring/excluding tasks from future use while preserving historical records.

CLI spellings/subcommands are illustrative when the existing CLI has a viable compatible equivalent. Preserve existing working entry points where possible.

## 3. Selection, task boundaries and intake

A candidate represents **one independently solvable, cohesive historical problem**, never an arbitrary diff chunk. It identifies a **base commit** (before the selected work), a **head commit** (where the work is complete), related intervening/follow-up commits, and sources of future evidence. Boundaries must reflect actual ancestry, including squash/merge/rebase situations; do not split one feature artificially or combine unrelated work.

A candidate records enough provenance to reconstruct the base and inspect future changes: source repository or local source reference, base/head SHAs, relevant commits/PRs, a problem/outcome description that **doesn't give away the implementation**, why it is useful, future test/evidence pointers, and any setup/cleanup risks. The CLI handles schema/validation, import, stable identity, duplicate awareness and storage. The producer's handoff can be converted; it need not mirror Mineval's internals.

Select high-signal engineering work: non-obvious bugs, fixup chains revealing hidden obligations, debugging across components, integration refactors and substantive features. Small subtle fixes can be valuable. Favor real or lightly mocked verification that meaningfully distinguishes correct behavior; don't optimize for number of tests or encourage superficial unit tests. Require a coherent explanation of what needed to be accomplished (including less obvious requirements) and a credible route to evaluate it. Reject or defer vague, unverifiable or uncleanable changes. **Preserve and adapt the existing `MINEVAL_MOMENT_SELECTOR.md` rather than replacing its useful selection heuristics with a few generic bullets.**

## 4. Task state, identity, versions and suites

- The **task pool grows** as candidates are imported. A task has a stable identity; prepared revisions are **immutable**. Changes to cleanup, verification or frozen environment create a new prepared revision. Older runs continue to point to their exact prepared revision.
- Maintain distinct statuses for candidates/drafts, ready or frozen revisions, rejected/needs-review and retired tasks as appropriate. Rejecting a preparation isn't the same as deleting the historical task record.
- Benchmark **suites** are immutable selections of exact task revisions (and any necessary basic evaluation policy). Adding tasks to the pool does not silently mutate suites or past results.
- A **run** is separate from a **task** and records the evaluated **model + harness** (e.g., model identifier and Codex adapter), source task revision, pertinent version/provenance and termination state. Do **not** build a ranking dimension around elaborate harness-configuration combinations; record minimal operational metadata necessary to understand/reproduce the run.
- Different task subsets are **not directly comparable**. Any comparison reports common-task coverage and the exact suite/task selection. Do not create automatic reruns or pretend that two models evaluated on different sets have comparable aggregate scores.
- Evaluation results have their own identity/version and point to the original run and verification revision; reevaluating never rewrites the prior verdict.

## 5. Historical-state boundary — critical revision to old specs

**Preserve Git history from the beginning of the repository through the selected base commit**, because `git log`, `git blame` and past diffs are authentic problem-solving tools. **Remove future Git commits/refs/objects**, including those reachable only via dangling objects, packed refs or reflogs, as well as future solution files or private verification. Merely checking out the base in a modern clone is **not enough**. Produce a historical repository/workspace restricted to the permitted past and validate its accessible history.

The benchmarked agent must not receive head/future solution patches, future tests, reviewer notes, private evidence, leaked `.git` objects, or a workspace mount from the actual developer project. Keep repository reconstruction/future evidence only in protected preparation storage. Repository identity/source links needed for intake remain private preparation metadata; avoid shipping convenient solution pointers to the benchmark agent. A baseline problem statement should describe the expected outcome rather than the implementation.

**Conflict note:** The original `MINEVAL_IMPLEMENTATION_SPEC.md` said the final archive contains **no `.git`**. Our later decision **supersedes that rule**: a benchmark starting workspace must retain a sanitized, valid **pre-base Git history**, with *no future history*. Choose an archive/bundle format that can safely represent this; do not keep the old no-Git assumption by accident.

## 6. Cleaning and fresh-environment checks

Cleaning changes only what is necessary to make the old base runnable in an isolated, reproducible environment. Never fix or partially implement the target task, alter its intended difficulty, remove required behavior or add filler tests. If necessary cleanup changes the challenge, mark the candidate for rejection/review. Record each meaningful cleanup adjustment with a reason and keep preparation changes separate from the eventual agent solution.

Retain or implement a fresh-environment **`mineval verify` diagnostic during preparation**: the cleaning agent can run it on a consistent copy of its current draft inside a **new disposable sandbox**, see command-level outcomes and continue editing the draft. This diagnostic is **not** a requirement that every pre-task test passes—the task may deliberately contain failing tests. Distinguish environment startup failures, unavailable commands, and expected behavior failures. Do not expose the host Docker socket to the agent.

Keep environment setup sufficiently reproducible (runtime/container identity, required dependencies and services, commands, data). If crucial infrastructure/secrets cannot be safely reproduced without changing the problem, reject or defer rather than silently weakening it.

## 7. Private verification extraction and independent review

A **separate extraction agent** inspects future commits and preserves relevant tests/fixtures/scripts/test-only helpers privately, with source references and explanations of what behavior they establish. It must not copy the future production solution into the benchmark snapshot. Verification extraction should prefer **focused meaningful private tests** for ambiguous features; the extractor may instead choose a **fuller verification package** for clear bugfixes or objectively specified work. Do **not** mandate an exhaustive verification package for every task. If nothing defensible can be extracted, flag it, do not invent tests that assert arbitrary implementation choices.

Where feasible, check whether selected verification can run against the prepared state and a known future-correct state; expected base failures can be legitimate. Record execution prerequisites and limitations. Avoid treating test count or test execution volume as a quality measure.

After cleanup and extraction, **another independent agent/session** reviews the preparation, separate from the agents that made it. It checks task preservation, historical/future leakage, test relevance, test quality, unjustified assumptions and whether a feasible benchmark exists. Reviewer output must include approve, revise/needs-review or reject, with concise findings. Only approved preparations become frozen, immutable revisions. No mandatory human approval for every task; humans can inspect and override by explicit action later.

## 8. Isolation, network access and Codex adapter

**The coding agent process itself must run in a disposable, isolated environment**, not just its test commands. Its writable project directory is a per-run disposable copy. Never mount the user's live project, host credentials/home, or a Docker socket into the agent environment; never provide GitHub write/push credentials. Use safe Docker isolation (prefer the project's established rootless Docker approach), and isolate private preparation/evaluation artifacts from benchmark-agent access. Treat open-network agents as untrusted: isolation protects the host/project; an instruction alone does not supply a security boundary.

**Permit ordinary outbound web access** because realistic coding agents consult documentation and other projects. Instruct agents **not to inspect the source project's future solution, commits or PRs**, but do not implement broad web/domain filtering as an anti-cheating mechanism. This is a **trust-based policy**, not a guarantee. Ask the agent at completion whether it saw the historical solution; save the answer as **self-reported**, not verified proof. Do not store full transcripts or build transcript auditing/replay.

Build **dedicated adapters** for needed harnesses, not a universal plugin ecosystem. Start with **Codex**, whose CLI is installed on the developer's host; do **not** assume the host binary can be used unmodified inside the sandbox. The adapter should implement the small internal capabilities needed to launch a model, deliver the prompt, collect completion/status and final workspace. Preserve ordinary subscription/auth workflows where safely possible; provide only necessary scoped model access and do not weaken sandbox isolation to make authentication convenient. Record blockers if isolation/auth cannot be achieved safely. Use a **fake adapter for almost all development tests**, and **never run repeated real Codex calls**. A single brief smoke test should require explicit user approval; limited Codex quota matters.

Prompt delivery and lifecycle details belong to the dedicated adapter; require consistent task content and isolation rather than trying to mandate identical internal harness operations. Basic timeout/cancellation/crash handling is necessary; **no elaborate budget or token/cost management in v1**.

## 9. Benchmark artifacts and evaluation

A run stores its exact task revision, model + harness, selected input/instruction identity, outcome/termination status, final workspace or diff, and the agent's brief solution-lookup self-declaration. **Do not retain agent transcripts or sensitive chat/session histories**. Avoid storing unrelated secrets in final workspace artifacts; clearly document the retention implications of code outputs.

Evaluation is a **separate operation** on a saved result, using private extracted verification applied to an isolated copy of the agent's work. Protect tests from overwrites by candidate files. An evaluator agent can adapt tests when alternative legitimate implementations call for it, but **every adaptation must preserve the underlying behavioral requirement**. Record its source, what changed, why it was necessary and what evidence supports the result. Don't relax expectations until an incorrect solution passes or impose obligations merely because the original historical implementation did something a particular way. Evaluator may reason about additional obligations grounded in the original task and evidence, but must justify them; arbitrary generated checks are not authoritative.

Minimum verdicts: **pass**, **fail**, **inconclusive** (the evidence cannot support a defensible conclusion, e.g., flaky or interface-tied checks), and **error** (evaluation infrastructure failed). Save structured findings, test outcomes, adaptations and reasons—not transcripts. Keep early scoring simple (e.g., verdict and pass rates over strictly comparable tasks), and support explicit re-evaluation with versioned findings. A verdict should show what was observed, not merely assert success.

## 10. Human observation, tests and delivery discipline

Provide a basic local interface to view the growing task pool, exact revisions and state, preparation changes/review, benchmark outputs/diffs, evaluations and evidence, and common-task comparisons. Enable task retirement/exclusion via a simple deliberate action or CLI, preserving old runs. **Observation only**: no session steering, `/resume`, live prompting, transcript viewer or custom agent terminal inside the UI.

Reuse the **existing repository, sandbox, CLI, tests and selection skill**. Make the smallest compatible changes. Do not run large automated test loops merely to look thorough. Use targeted deterministic unit/integration fixtures and the fake harness, plus a small isolated end-to-end example before declaring the tool usable. Do not equate passing fixture tests with proof that live Docker+Codex works; distinguish tested/untested paths in the handoff.

Keep each development phase separate. Read this contract and prior phase handoff; implement **only** the requested phase; save what changed, what was tested and still broken, and any new small implementation choices. Do not silently revise agreed policy. Defer public marketplaces, global leaderboards, statistical ranking, broad model-adapter catalogues, parallel/distributed execution and interactive human probing until needed.
