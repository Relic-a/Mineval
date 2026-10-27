---
name: mineval-moment-selector
description: Select cohesive historical benchmark tasks from any Git project when asked to use Mineval; inspect future evidence and hand candidates to the local CLI. Does not prepare or run benchmarks.
---

# Mineval Moment Selector

## 1. Purpose

When invoked by **any task-selection agent or general-purpose harness**, scan a Git repository's history to identify **historical task moments** that will make strong Mineval benchmarks. A moment consists of an **unsolved starting state** and **later work that reveals the required outcome**. Prioritize tasks that require investigation, engineering judgment, or discovery of non-obvious requirements—not just matching a known patch.

Mineval has a preparation pipeline: a later cleanup agent makes the starting state runnable **without solving the task**, and a separate extraction agent preserves **task-relevant verification from future commits**, privately. Your selections must be feasible for both steps. **Do not implement those steps in this skill.**

**This skill is independent of Mineval's runtime.** Do not assume the selecting agent is a built-in Mineval component, uses a particular AI harness, has a particular GitHub integration, or saves results into a Mineval-defined directory. The invoking agent/harness decides how to obtain repository history, how many tasks to collect, and how/where to save or hand off the selected tasks. Use local Git commands (`git log`, `git show`, `git diff`, `git merge-base --is-ancestor`) and full SHAs; GitHub APIs are optional. Work offline when the necessary objects are local. This skill defines **what makes a task worth selecting** and the CLI handoff below. Do not start background discovery or modify the source project.

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
3. Screen changes with **more than 1,000 lines of substantive logic** carefully; defer if they cannot be bounded as one reproducible, cohesive problem. Do not count automatically generated lines as logic.
4. Avoid massive dependency migrations or change sets where relevant behavior cannot be isolated.
5. Size is advisory, never an automatic rejection rule. Do not reject a task solely because its diff is short: **reasoning difficulty, task clarity, and testability matter more than raw size**. Record any exception to the defaults.

## 5. Inspect Future Commits for Verification Evidence

**This is a required part of selecting a moment, not something to defer entirely to the extraction agent.** Do a feasibility inspection, not the extraction itself.

1. Examine the task-related future diff, PR discussion, and associated follow-up commits. Identify **new or modified tests**, regression cases, fixtures, test-only helpers, runnable examples, benchmark workloads, or other executable checks.
2. Prefer meaningful real or lightly mocked checks that exercise the behavior; avoid superficial assertion counts. For each useful check, record its **source commit SHA**, path, and the behavior or metric it establishes. Capture which requirements are only revealed by later fixup commits.
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

## 9. Selection Record and CLI Handoff

Write one JSON object per candidate in a normal file outside the source tree. The
agent selects; `mineval task add` validates and packages local historical material.
Never arrange Mineval's internal directories yourself. A portable example (replace
placeholder SHAs with full lowercase object IDs):

```json
{
  "schema_version": 2,
  "repository": "/local/project",
  "base_source": {"type": "git", "path": "/local/project"},
  "base_commit": "<sha-before-feature>",
  "head_commit": "<sha-after-related-fix>",
  "related_commits": ["<feature-sha>", "<fix-sha>"],
  "related_prs": [],
  "category": "integration",
  "signal_patterns": ["fixup-chain"],
  "task_summary": "Support batched requests, including empty batches, while preserving single-request behavior.",
  "why_high_signal": "A later regression exposed an overlooked boundary case.",
  "size": {"source_files": 2, "substantive_logic_lines": 24},
  "future_evidence": [
    {"commit": "<later-test-sha>", "path": "tests/test_batch.py",
     "proves": "Empty batches return an empty result and mixed batches preserve order."}
  ],
  "execution": {
    "runtime": "Python 3",
    "likely_commands": ["python -m unittest discover -s tests"],
    "external_requirements": [],
    "cleanup_risks": []
  },
  "verification_feasibility": "Public behavioral checks can exercise independent implementations without solution helpers; portability has not been tested.",
  "uncertainty": [],
  "selection_status": "accept",
  "selection_reasons": ["Cohesive feature with a meaningful follow-up regression."]
}
```

The summary must describe observable outcomes and include obligations established
by follow-up fixes. Keep algorithm names, patch instructions, source URLs and
historical solution details in private selection notes, outside the summary.

From any project, with Mineval installed:

```sh
mineval --data /absolute/private/mineval-data task add /path/to/candidate.json
mineval --data /absolute/private/mineval-data task show TASK_ID_FROM_OUTPUT
```

`base_source.path` is a local Git repository; relative source paths resolve against
the JSON file. No remote API, network access or model call is required by intake.
The repository identity may be a local path or URL. Use an absolute private store
outside the source project. The CLI copies the base into a draft and captures
head, related commits and evidence commits privately, checking base ancestry and
evidence file existence. Tests may follow head: list their actual commit in
`future_evidence`; do not move the base to include them. For squash/merge histories,
record related commits only when they descend from the selected base; explain
alternate provenance in notes rather than inventing ancestry.

`task_id` is optional: omission assigns a deterministic ID from repository identity
and boundaries; save the returned ID. Explicit persistent IDs remain supported.
Re-adding the same ID fails without modifying existing artifacts. Changing source
identity or boundaries changes the automatic ID; intake does not detect semantic
duplicates across aliases. Inspect the pool before collecting overlapping tasks.

For evidence without a file, provide `commit`, `proves`, and `reproduction` containing
concrete reproducible verification instructions. This records a feasibility claim;
it does not execute the instructions. Full local historical objects must be available
for accepted Git imports. A PR link alone cannot substitute for commits.

If important facts are missing, use `selection_status: "defer"`, omit unknown fields
and list questions in `uncertainty`. Do not fill unknown SHAs with placeholders in a
real handoff. The CLI retains deferred/incomplete boundary records as `needs-review`
without a draft or historical export. `reject` records stay inspectable as rejected.
Malformed supplied SHAs, invalid types, unsafe evidence paths, missing objects and
false ancestry in accepted records fail rather than becoming invented facts.
Legacy imports without detailed evidence notes remain supported; their intake
report explicitly marks missing notes as incomplete, without claiming readiness.
Deferred records are immutable intake records: submit a corrected candidate under
an explicit new ID and note the prior ID; there is no in-place resume command yet.

**Keep full future history, solutions, and verification evidence outside the
benchmarked agent's starting snapshot.** Discovery may inspect future material.
This skill does not clean, extract private tests, evaluate solutions or launch a
benchmark. Sanitized pre-base history is a later preparation responsibility; current
intake drafts contain only base files. Never pass a private store or source clone
to a benchmarked agent.

## Required primary category

Every newly selected candidate must use `schema_version: 2` and include `category`, chosen from this fixed list.
Choose the primary engineering obligation, not the type of commit (bug/feature/refactor).
Use exactly one; do not invent categories:

- `behavior`: core domain behavior, algorithms, parsing and computation.
- `integration`: APIs, protocols and interoperability between components.
- `data`: storage, queries, migrations, serialization and data integrity.
- `reliability`: concurrency, performance, resource management and recovery.
- `security`: authentication, authorization and trust boundaries.
- `developer-tools`: builds, packaging, configuration and development workflows.
- `user-interface`: interaction, rendering, accessibility and presentation.

For overlap select the area whose requirements principally establish correctness.
Old handoffs without a category remain importable as uncategorized; do not infer a
historical label from a task title.
