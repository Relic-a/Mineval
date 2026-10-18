---
name: mineval-moment-selector
description: Identify high-signal Git PRs and commit chains with future evidence and reproducible verification for Mineval benchmark tasks.
---

# Mineval Moment Selector

## 1. Purpose

When invoked by **any task-selection agent or general-purpose harness**, scan a Git repository's history to identify **historical task moments** that will make strong Mineval benchmarks. A moment consists of an **unsolved starting state** and **later work that reveals the required outcome**. Prioritize tasks that require investigation, engineering judgment, or discovery of non-obvious requirements—not just matching a known patch.

Mineval has a preparation pipeline: a later cleanup agent makes the starting state runnable **without solving the task**, and a separate extraction agent preserves **task-relevant verification from future commits**, privately. Your selections must be feasible for both steps. **Do not implement those steps in this skill.**

**This skill is independent of Mineval's runtime.** Do not assume the selecting agent is a built-in Mineval component, uses a particular AI harness, has a particular GitHub integration, or saves results into a Mineval-defined directory. The invoking agent/harness decides how to obtain repository history, how many tasks to collect, and how/where to save or hand off the selected tasks. This skill only defines **what makes a task worth selecting** and which provenance/evidence to preserve.

## 2. Define the Commit Boundary

Every selected moment needs an unambiguous task boundary:

1. Identify **one cohesive unit of work**, such as a merged PR or a closely related commit sequence (including relevant follow-up fixes).
2. Set `base_commit` to the last repository state **before** the selected work begins (normally the parent of the first task commit on the applicable history). It must still have the target problem or lack the target feature.
3. Set `head_commit` to the state where the selected work is complete. Record all intervening task-related commit SHAs and the relevant PR/issue references.
4. Inspect commits **after `base_commit`**, including later regressions or follow-up tests when relevant. These future commits are the evidence for what the task actually required; they are not part of the evaluated agent's starting snapshot.
5. Avoid mixing unrelated changes into the task. If a PR contains unrelated work, isolate an accurate boundary or reject it if isolation would misrepresent the task.
6. Do not split one logical feature into multiple artificial tasks or combine unrelated features into one.
7. If merge strategy, rebases, cherry-picks, or squashes make ancestry ambiguous, resolve the actual code-state boundary and document it. Do not assume `head_commit^` is always the correct base.

**Example:** A introduces a faulty state, B implements a feature, C fixes its null handling, and D adds a regression test. Depending on the intended task, Mineval may select the pre-B state as the base, include B–C as the task's work, and use verification from C–D. It must not put C or D's solution code into the starting state.

## 3. Select High-Signal Moments

Accept moments strongly matching one or more of these patterns:

### A. Fixup Chains (Hidden Obligations)

- Find a feature or fix followed by further task-related corrections: e.g., null payload, error cleanup, race handling, retries, state rollback, edge cases.
- The **later corrections** reveal requirements the first attempt missed. They should be relevant to the same objective, not an unrelated new feature.
- Why: Tests whether a model anticipates non-obvious obligations on its first attempt.

### B. Root-Cause Debugging

- Prefer bugs where the observed symptom and actual cause are separated by modules, abstraction boundaries, or asynchronous flows.
- Look for evidence of the reported behavior and a concrete way to reproduce or assert the fix.
- Why: Forces investigation rather than superficial single-file edits.

### C. Architectural Trade-Offs

- Look for changes involving state ownership, interface design, concurrency, error handling, caching, or other nontrivial design choices.
- Require observable behavioral or structural constraints; **architectural taste alone is not verifiable**.
- Why: Multiple valid implementations can reveal differences in model judgment.

### D. Human Rescue of Agent Work

- Look for **documented** cases where an AI agent's initial code was subsequently corrected or refactored by a human.
- Confirm provenance through explicit PR descriptions, commit messages, review discussion, or other strong evidence. Do not infer AI authorship from code style.
- Why: This may expose real failure modes in an earlier coding agent.

Also consider performance and optimization tasks when later commits demonstrate a **measurable objective** (latency, throughput, memory, allocation count, etc.) **and** a way to check correctness. These must have reproducible workloads or other usable evidence; vague “faster” claims are not enough.

## 4. Apply Scope Filters

Apply these as **default screening heuristics**, then assess actual complexity and cohesion:

1. Count modified **source** files, excluding lockfiles, generated files, snapshots, and bulk formatting changes. Prefer **2–15 source files**.
2. Prefer at least **30 changed lines of substantive logic**. An exceptionally high-signal one-file or smaller change (e.g., subtle concurrency/state fix) can still qualify if documented.
3. Reject changes with **more than 1,000 lines of substantive logic** unless a clearly isolated, cohesive task can be bounded smaller. Do not count automatically generated lines as logic.
4. Avoid massive dependency migrations or change sets where relevant behavior cannot be isolated.
5. Do not reject a task solely because its diff is short: **reasoning difficulty, task clarity, and testability matter more than raw size**. Record any exception to the defaults.

## 5. Inspect Future Commits for Verification Evidence

**This is a required part of selecting a moment, not something to defer entirely to the extraction agent.** Do a feasibility inspection, not the extraction itself.

1. Examine the task-related future diff, PR discussion, and associated follow-up commits. Identify **new or modified tests**, regression cases, fixtures, test-only helpers, runnable examples, benchmark workloads, or other executable checks.
2. For each useful check, record its **source commit SHA**, path, and the behavior or metric it establishes. Capture which requirements are only revealed by later fixup commits.
3. Determine whether future tests are actually relevant to the selected task, rather than unrelated changes that happened nearby.
4. Check whether the verification could reasonably be used against an independently written solution, instead of requiring exact implementation details from the historical patch.
5. Notice dependencies introduced in future commits. Separate **test infrastructure** (fixtures, helpers, test dependencies) from **production implementation**. If extracting the checks requires copying solution code into the starting workspace, the moment may be unsuitable.
6. Prefer moments with clear, executable future checks. When no tests exist, require some other **concrete, reproducible basis for verification**, not a subjective promise that code inspection will suffice. If there is no credible way to establish completion, reject the task.
7. For optimization work, look for a reproducible workload/metric **and** correctness/regression checks. A performance claim without an executable measurement path is insufficient.
8. Record limitations or uncertainty. Do **not** claim the tests pass, fail, or are portable unless you actually ran them. The later preparation stages will establish this.

The discovery agent **does not extract, apply, adapt, or execute hidden tests as a grading system**. It identifies the evidence that the later extraction stage should preserve.

## 6. Check Execution and Cleanup Feasibility at `base_commit`

Before accepting a moment, assess whether the base state can become a reproducible **Linux/rootless-Docker workspace** without changing the task:

1. Identify a plausible runtime, build command, test runner, and/or smoke command. An existing working build is ideal, but a reliably reconstructible setup also qualifies.
2. Confirm the target behavior is reachable or observable through an API, CLI, integration test, internal test seam, user flow, or measurable workload. It need not be a *public* API.
3. Identify special requirements: databases, services, platform-specific tooling, paid APIs, secrets, proprietary binaries, GPUs, datasets, or external network calls.
4. **Network access may be available** in Mineval's verification environment. Do not reject tasks merely for using the network; assess whether required services and inputs are realistically available and repeatable.
5. Determine which unrelated broken tests, setup steps, dependencies, or integrations might need to be removed/adjusted by the cleanup agent.
6. Ask whether those changes risk **fixing, partially implementing, bypassing, or removing the target task**. If they do, **reject** the moment rather than weaken the task.
7. Favor deterministic verification that will not depend on private credentials, unpredictable live services, inaccessible accounts, or manual human actions. Reject tasks that depend on these *when they cannot be safely replaced without changing the work being benchmarked*.

This stage is a feasibility screen, **not full environment repair**. Don't require all current tests to pass: some failures are the bug the agent is meant to solve, and others can be diagnosed during cleanup.

## 7. Reject Low-Signal or Unverifiable Work

Reject candidates dominated by:

- Mechanical renames, file moves, import rewrites, or formatting sweeps without meaningful behavior changes.
- Dependency, configuration, CI, or linter updates **without** a substantial engineering problem. A dependency/config change can be acceptable when the underlying task genuinely tests nontrivial behavior.
- Documentation, typo fixes, or static-text edits with no engineering decision or non-obvious behavior.
- Tasks requiring unreplaceable private credentials, inaccessible infrastructure/hardware, or non-repeatable external services.
- Commit histories too entangled to define an honest base/task boundary.
- Tasks for which there is **no credible path to objective verification** from future evidence or observable behavior.
- Tasks where sandbox cleanup would remove or substantially alter the target problem.

## 8. Final Selection Checklist

Answer all of these before accepting:

1. **Decision:** Must the model investigate a non-obvious cause, satisfy hidden obligations, or make a meaningful engineering decision?
2. **Alternatives:** Could a stronger model solve the same task differently or better, rather than merely reproducing an exact patch?
3. **Future evidence:** Which later commit(s) establish what the task required, and which specific verification artifacts or observable outcomes support that claim?
4. **Verifiability:** Is there a credible, reproducible way to determine completion for an independently implemented solution?
5. **Cleanability:** Can the base state run under Mineval's planned environment after **task-preserving** cleanup? If cleanup touches the objective, would we reject it?
6. **Boundary:** Is the selected task cohesive, with an accurate `base_commit` and `head_commit` and no solution leakage into the starting state?

Only accept if all six answers are satisfactory. Unknowns must be explicitly flagged; a serious unknown in cleanability or verifiability means reject or defer, not silently accept.

## 9. Selection Record to Hand Off

For each candidate, produce enough information that a **separate** preparation system can reconstruct the historical starting state and inspect future verification evidence. Follow the invoking harness's output/storage format if one exists. Otherwise, use a concise, inspectable record. The following YAML is only an example; **do not require Mineval-specific storage paths, APIs, or an internal task collector**:

```yaml
repository: https://github.com/owner/repo
base_commit: <sha-before-task>
head_commit: <sha-where-task-complete>
related_commits: [<task-commit-shas>]
related_prs: [<pr-identifiers>]
category: bugfix                  # e.g., feature, refactor, optimization
signal_patterns: [fixup-chain, root-cause-debugging]
task_summary: <outcome to achieve; do not reveal exact solution>
why_high_signal: <brief rationale>
size:
  source_files: 5
  substantive_logic_lines: 170
future_evidence:
  - commit: <sha>
    path: tests/test_regression.py
    proves: <specific expected behavior>
  - commit: <sha>
    path: tests/fixtures/cases.json
    proves: <relevant edge-case input>
execution:
  runtime: <language/toolchain>
  likely_commands: [<build-or-smoke-command>]
  external_requirements: [<services/network/secrets/hardware>]
  cleanup_risks: [<potential interference with target task>]
verification_feasibility: <why extracted evidence can grade independent solutions>
selection_status: accept          # accept | reject | defer
selection_reasons: [<short reasons>]
```

**Keep full future history, solutions, and verification evidence outside the evaluated agent's starting snapshot.** This skill selects and records candidates; later agents do task-preserving cleanup and private verification extraction. It does not choose a model harness, dictate task-collection implementation/storage, or implement post-run adaptive grading.
