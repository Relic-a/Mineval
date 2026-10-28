# Operations

1. Install using the repository's `docs/installation.md`. Standalone release candidates
   must be built and published before the installer can download them. Python users
   can install a built wheel with pipx, uv tool install, or pip.
2. Choose a local rootless Docker context. Run
   `mineval --context CONTEXT --data /absolute/store setup --image TRUSTED_IMAGE --auth-file /absolute/model-only/auth.json`.
   This saves paths, checks prerequisites and installs nothing. Resolve each diagnostic;
   `mineval doctor` must report ready. Image requirements depend on the repository;
   see `examples/Dockerfile.preparation`. Codex-backed operations need Codex in the image.
3. With mineval-moment-selector, produce candidate JSON, then `mineval import candidate.json`.
   `mineval task list` and `mineval task show TASK` inspect registrations.
4. `mineval workflow prepare-all` queues eligible tasks. `mineval workflow work` executes
   isolated cleanup, verification extraction and independent approval before freezing.
   Save trusted task evaluation commands in `historical-settings.json` in the data
   directory, following `docs/historical-workflows.md`. Commands are never guessed.
5. Inspect the immutable revision with `mineval task revision --help`. Submit a JSON
   workflow definition using `mineval workflow submit request.json`:
   ```json
   {"kind":"benchmark","selections":[{"task_id":"TASK","version":"VERSION"}],"configurations":[{"model":"MODEL","effort":"medium","adapter":"codex"}],"exclude":true}
   ```
   Use `mineval models` for provider model discovery without inference (requires a host
   Codex CLI), or an explicit configured identifier. Use an available explicit model identifier and supported reasoning effort from
   `mineval bench run --help`. Set preparation/evaluation/review models in trusted
   settings. `mineval workflow work` runs queued stages; `mineval workflow list` monitors.
6. Inspect `mineval bench show --help`, `mineval evaluation show --help`, and
   `mineval model-review history MODEL`. Compare explicit selections using
   `mineval evaluation compare definition.json`; see `examples/evaluation-comparison.json`.
   Pass/fail describe private checks; inconclusive/error do not imply a solved/failed task.
   Reports retain policy, provenance and adaptation limitations.
7. On interruption run `mineval workflow work` to mark abandoned running jobs interrupted,
   then `mineval workflow retry JOB`. Usable saved runs are reused; interrupted evaluations
   get new immutable IDs. Use `--new-attempt` only when there is no usable run output and
   the user authorizes another benchmark. Review failure requires only a review retry.
8. Run `mineval workflow maintain` independently (for example from a user timer) to drain
   durable memory compaction requests. It uses the worker's saved model-only authentication.
   Failures preserve observations; expired reservations can be reclaimed on a later run.
   This command is separate from result publication and benchmark workers.

For cache issues use `mineval cache status`. Pruning with `mineval cache prune --max-mb N`
removes reusable entries, not benchmark evidence. Integrity mismatches require investigation;
never bypass validation. Resource admission can leave jobs queued; reduce workload limits
or free resources, then run the worker again. Configuration overrides use `--settings` on
workflow/observe; explicit CLI globals override saved context/data.
