---
name: mineval
description: Install, configure, diagnose and operate Mineval historical coding benchmarks through its CLI, including preparation, evaluation, comparisons and recovery. Use the separate mineval-moment-selector skill for historical task discovery.
---

Mineval measures coding models against real historical software changes. A task has
an isolated starting tree and private future verification. A frozen revision pins
both. A run saves a candidate; an evaluation independently tests it; qualitative
review records observations without changing the verdict.

Install this folder in your agent skills directory (`~/.codex/skills/mineval`
or `~/.claude/skills/mineval`). Keep the source checkout docs/examples available;
these references describe repository-relative paths, not files inside the executable.

Start with `mineval --help`, `mineval setup --help`, and `mineval doctor`. Read
[operations](references/operations.md) for the end-to-end procedure. Use CLI JSON
output; no dashboard is required. Global `--data` and `--context` precede the command.
Saved setup settings supply their defaults. Preserve explicit user overrides.

Use the existing **mineval-moment-selector** skill to discover historical moments
and generate candidate JSON; preserve that skill and its evidence discipline.
Do not invent task definitions from the future patch and give them to the coding
model. Benchmark models receive only the frozen starting workspace and task statement.
Future commits, solutions and private checks belong solely to preparation/evaluation.

Require explicit user authorization before model calls with cost, downloads/image
builds, external publication, credential selection, new benchmark reruns, or retiring
tasks. A user request to execute a specified benchmark authorizes its required model
stages. Never print credential contents, forward host-wide credentials, edit receipts,
reuse an uncertain identity or mutate frozen evidence to get a passing result.

Read the installed command help before selecting flags. Stop and report an unsupported
runtime rather than weakening rootless Docker checks. Report coverage and infrastructure
errors separately from task failures; compare only explicit common prepared revisions.
