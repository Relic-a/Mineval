# Mineval decisions

Mineval is a local-first CLI with independently invoked task add, prepare, bench run,
and evaluate operations. There is no required server, queue, scheduler, or distributed
control plane. Discovery belongs to an external skill that supplies candidates through
the CLI; the task pool can grow without changing existing suites.

- **Identity and immutability:** the caller supplies a persistent task ID. Re-adding
  that ID fails. Existing frozen `versions/<version>` are prepared revisions; preparing
  again publishes a new version. Suites explicitly pin ordered task/version pairs and
  their snapshot and artifact-manifest hashes. New membership requires a new suite ID.
- **Minimal cleanup:** preparation adapts the environment while preserving the task.
  Historical failing tests are allowed; cleanup must not solve or bypass the objective.
- **Private verification:** future evidence and extracted checks stay host-private and
  outside evaluated-agent workspaces. Evaluation will be separate from preparation
  diagnostics and benchmark execution.
- **Independent reviewer agent:** the intended preparation workflow uses a reviewer
  agent separate from the cleanup agent. Current finalization records explicit review
  attestations; independent reviewer orchestration/enforcement is deferred.
- **Historical Git:** agents should retain history through the base commit while future
  commits, refs, and objects remain hidden. Current exports contain no `.git`; safely
  exposing historical history is deferred. Do not mount the original repository.
- **Isolation and web:** each agent gets an isolated workspace/container with normal
  web access, subject to explicit network configuration. Isolation does not make future
  solutions undiscoverable on the public web. No credential forwarding is implied.
- **Harness adapters and identity:** use dedicated adapters for supported harnesses.
  Future run records must identify both model and harness (including relevant versions
  and configuration), pin a prepared revision, and distinguish failures from completed
  work. A generic sandbox command is not a benchmark run.
- **No stored agent transcripts:** retain structured provenance, configuration, results,
  and necessary verification diagnostics, not agent conversation transcripts. Existing
  diagnostic command stdout/stderr remain diagnostic evidence.
- **Human inspection:** use readable JSON, diffs, hashes, and archives with CLI inspection.
  Immutability is enforced by application behavior, read-only files, and integrity
  receipts, not protection against deliberate changes by the owning OS user.

This phase extends existing intake/preparation, adds task inspection and immutable
suites, and reserves task-local `runs/` and `evaluations/` namespaces. `bench run` and
`evaluate` fail explicitly without creating records. Run/evaluation schemas, adapters,
reviewer automation, historical Git exposure, and evaluation policy remain later work.
