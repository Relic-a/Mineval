# Installation and setup

Supported execution host: Linux x86_64, local rootless Docker, cgroup v2 and systemd
with delegated CPU, memory, swap and PID controllers. Docker Desktop, remote Docker,
macOS and Windows are not supported execution hosts. Install Git and Docker using
trusted OS/Docker instructions; Mineval never installs services or changes contexts.

## Alpha release

The [0.1.0a5 prerelease](https://github.com/Relic-a/Mineval/releases/tag/v0.1.0a5)
contains Python wheel and source packages. Python 3.11+ is required. With GitHub CLI
authenticated to an account that can read this private repository:

```sh
gh release download v0.1.0a5 --repo Relic-a/Mineval --pattern '*.whl'
pipx install ./mineval-0.1.0a5-py3-none-any.whl
```

This alpha is intended for early testing; fresh-host acceptance and a paid end-to-end
benchmark remain unverified. Standalone builds pass CI smoke checks, but GitHub does
not support provenance attestations for user-owned private repositories. No standalone
binary or installer asset is published in this alpha; use the Python package above.

## Standalone release candidates

The repository includes a standalone build workflow and the following installer for
future attested releases. Its verification requirement remains mandatory.
The installer requires sha256sum and GitHub CLI `gh` for authenticated downloads and
cryptographic GitHub artifact provenance verification. No Python is needed. Downloads
are versioned; verification fails closed. The default destination is `~/.local/bin`.
For private repository access, first authenticate with `gh auth login` using an account
that can read this repository.
Add that directory to PATH yourself if needed; the installer edits no shell files.
Repeat with another version to upgrade; `sh install.sh uninstall` removes only the
managed CLI and retains settings and evidence. Never remove your data store to upgrade.

Manual installation: download `mineval-vVERSION-linux-x86_64` and its `.sha256` from
`https://github.com/Relic-a/Mineval/releases`. Verify with
`gh attestation verify ARTIFACT --repo Relic-a/Mineval --signer-workflow Relic-a/Mineval/.github/workflows/release.yml --source-ref refs/tags/vVERSION`,
then `sha256sum --check ARTIFACT.sha256`. Install with `install -m755 ARTIFACT ~/.local/bin/mineval`.
Do not execute unverified downloads.

The executable bundles CPython and web assets with PyInstaller. Linux builds target
Ubuntu 22.04 (glibc 2.35+) on x86_64; musl and ARM are currently unsupported.
One-file extraction requires an executable temporary directory. Optional Arena
image sharing dependencies are not bundled. Builds pin Python and top-level build
tools but are not yet proven byte-for-byte reproducible; CI artifacts are candidates,
not automatically uploaded releases. Build provenance is separate from semantic trust.

## First run

```sh
mineval --context mineval-rootless --data "$HOME/.local/share/mineval" setup \
  --image mineval-preparation:local --auth-file /absolute/model-only/auth.json
mineval doctor
mineval observe
```

Create the context/rootless daemon beforehand. With approval for image downloads/builds,
build the starter: `docker --context mineval-rootless build -f examples/Dockerfile.starter -t mineval-preparation:local .`.
This maintained starter recipe contains the diagnostic client, Git, Python, Node and
a pinned Codex CLI. It requires no host npm installation. Add project-specific
toolchains in a derived trusted image; it is not a universal project environment.
The image recipe is provided, but no public image is assumed to be published. Authenticate the model separately, supplying
only its model-specific credentials. Never bake credentials into an image.

Codex-backed images must include a runnable `codex-code-mode-host` beside the
resolved Codex executable or on PATH, with its runtime dependencies. Copying only
the standalone `codex` binary is insufficient. `mineval doctor` probes both
executables offline, and each Codex run repeats the probe before contacting the
model. When replacing an incomplete image, update the saved default and existing
drafts' `control/verify.json` image references; changing the default does not
replace a draft's existing runtime configuration.

Setup is noninteractive and saves host-only paths in `$XDG_CONFIG_HOME/mineval/config.json`
(default `~/.config/mineval/config.json`, mode 0600). It reports missing requirements
and does not pull images. Re-run with explicit options to update. Existing stores
are not moved. `MINEVAL_DATA` and explicit `--data` override saved data selection;
without setup the legacy relative `mineval-data` default remains.

See [historical workflows](historical-workflows.md) for evaluation configurations and
end-to-end CLI requests. Admission estimates are separate from Docker's enforced
CPU, RAM, swap, PID and tmpfs limits; changing admission never changes frozen limits.
Tmpfs is charged inside container RAM, not added to it. Stage, total and per-model
concurrency limits still apply. The worker starts one job at a time, observes a fresh
interval, then rechecks available RAM and CPU capacity before adding
another. Preparation reserves space for both the paused agent and broker verifier.

The optional `resources` object in historical settings accepts:

| Setting | Default | Meaning |
| --- | ---: | --- |
| `cpus_per_job` | 2 | CPU admission estimate, independent of Docker quota |
| `memory_mb_per_job` | 2048 | RAM estimate per container, MiB |
| `reserve_memory_mb` | 512 | Host headroom kept free |
| `admission_interval_seconds` | 1 | Minimum time between starts |
| `memory_spike_mb` | 256 | Additional growth allowance per live container |
| `memory_spike_factor` | 1.5 | Growth multiplier on current container RAM |

Available RAM already excludes live allocations. For each active container the worker
reserves the greatest of its estimate, observed peak, or current RAM times the growth
multiplier plus the spike allowance, subtracting only measured anonymous/tmpfs allocations already
charged to the host. Reclaimable file cache receives no credit. Unreadable, remote or
stale measurements retain the full estimate. Enclosing cgroup quotas and memory
budgets also constrain admission. This is a conservative heuristic, not a guarantee
that every job can grow to its hard limit simultaneously. Increase estimates or host
headroom for unusually spiky workloads; reduce estimates only with measured evidence.

Normal runs read local cgroup counters about once a second. Results include peak RAM
(kernel `memory.peak` when available), cumulative CPU seconds, OOM kills and observed
CPU and memory PSI. Stage observations are also saved under `<data>/resource-usage/`, outside
benchmark receipts. Measurements are best effort: short-lived or inaccessible cgroups
can be missed, and sampled peaks are lower bounds if the kernel peak is unavailable.
There are no profiling runs or automatic tuning. PSI is telemetry only: it neither
blocks admission nor changes a verdict. OOMs and timeouts remain infrastructure
errors. The legacy `cpu_pressure_percent` and `memory_pressure_percent` settings
are accepted for compatibility but ignored. Missing telemetry never changes a
workload result.

When no workflow jobs are running, one eligible preparation job is attempted
regardless of soft resource estimates, including after each completion.
Additional concurrent jobs still require resource admission. Docker limits, runtime
checks and isolation always apply; execution failures are reported rather than left
queued. Other jobs that do not fit remain queued until capacity is available. Provider limits are operator configured.
Run `mineval workflow maintain` in a separate process for background compaction.

## Python distribution and development

Build with `python -m build`. Install the wheel with `pipx install dist/mineval-*.whl`,
`uv tool install dist/mineval-*.whl` or `python -m pip install dist/mineval-*.whl`.
PyPI publication is a separate authorized maintainer step; do not assume it exists.
For contributors: Python 3.11+, `python -m pip install -e .`, and
`python -m unittest discover -s tests`. Build executables using
`scripts/build-standalone.sh` with PyInstaller 6.22.0. Do not use editable installation
as the end-user distribution.

## Install the agent skill

Copy the repository's `skills/mineval` directory to `~/.codex/skills/mineval` for
Codex, `~/.claude/skills/mineval` for Claude Code, or your agent's skill directory.
Keep a copy of this repository's docs/examples available to the agent. This is
separate from and does not replace `mineval-moment-selector`.

To obtain the starter recipe and agent skill after binary installation:

```sh
git clone https://github.com/Relic-a/Mineval.git
cd Mineval
# Review the recipe, then approve its image/toolchain downloads before building.
docker --context mineval-rootless build -f examples/Dockerfile.starter -t mineval-preparation:local .
```
