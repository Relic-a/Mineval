# Phase 2 — external discovery skill and reliable CLI import

- **Implemented:** Portable [Moment Selector](../../skills/mineval-moment-selector/SKILL.md),
  preserving the original detailed heuristics in sections 1–8 and adding a JSON/CLI
  handoff. The original Markdown entry point links to the canonical skill. No
  discovery runtime was added. `mineval task add candidate.json` and `import` share
  `intake.load_candidate`/`normalize` and `Store.register`. Optional IDs are assigned;
  supplied IDs remain supported. Related/head/evidence commits are copied privately,
  with checked base ancestry and evidence file existence. Cleanup risks, follow-up
  obligations, uncertainty and reproduction instructions stay in private metadata.
  README, example and foundation docs describe the handoff. Copied the supplied v2
  decisions and handoff template into `docs/` as directed by the revised plan.
- **Decisions made during this phase:** Automatic IDs hash repository identity and
  base/head boundaries (legacy headless records use sorted future commits); incomplete
  boundary IDs also include the summary. Re-adding an ID fails. Repository aliases
  are not semantically deduplicated. CLI source paths resolve relative to the JSON
  file; Store API paths retain existing process-relative behavior. Deferred or
  missing-boundary candidates retain metadata/state/receipt only, as `needs-review`;
  selector rejections are `rejected`. Neither creates a draft or imports history.
  Operational entry points block them. Accepted legacy records lacking detailed
  notes still create drafts, explicitly marked incomplete in `metadata.intake`.
  This completeness report is not review approval. Correct deferred records by
  adding an explicitly new ID with a reference to the old ID; no mutation/resume API
  was added. Selection-quality judgments and evidence sufficiency remain agent work.
- **Checks performed:** `python -m unittest discover -s tests -v`: 52 tests,
  49 passed, 3 live Docker checks skipped. Five new deterministic local Git/CLI cases
  cover a batch feature, later empty-batch fix and still later test commit; demonstrate
  that the cited regression fails before the fix and passes afterward; preserve exact
  base/head/evidence and private source copies after source deletion; prove only base
  files enter the draft; exercise deferred/rejected records, duplicate identities,
  reproduction-only evidence, malformed records, unsafe/missing paths and unrelated
  ancestry with rollback. Skill quick validation and `git diff --check` passed.
  No live Docker integration or Codex/model calls were run.
- **Remaining limitations/blockers:** Current historical exports still use the legacy
  tree-only codec without `.git`. Sanitized pre-base history remains required by v2
  and must be implemented in the later isolation/preparation work; future snapshots
  and diffs are preparation-only. No remote fetch, GitHub API, automatic selection,
  cleanup, extraction, evaluation or benchmark execution was introduced. Source
  symlinks/submodules and existing artifact bounds retain their prior constraints.
  Deferred records retain references rather than copied source objects; preserve the
  local source until a corrected import succeeds. Copying the skill folder is the
  portable distribution mechanism; it was not globally installed.
- **Inputs for next phase:** `skills/mineval-moment-selector/SKILL.md`,
  `examples/candidate.json`, `mineval/intake.py`, `Store.register`, and the existing
  private `tasks/<id>/private/{base,future,diffs}` artifacts. New metadata carries
  `intake.schema_version: 1`, completeness/missing fields, exact future commit list
  and grounding status. Existing task/suite/revision/run references were not rewritten.
  Phase 3 can build on these boundaries and the current sandbox; Phase 2 stops here.
