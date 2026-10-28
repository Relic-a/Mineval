# Installation and setup

Supported execution host: Linux x86_64, local rootless Docker, cgroup v2 and systemd
with delegated CPU, memory, swap and PID controllers. Docker Desktop, remote Docker,
macOS and Windows are not supported execution hosts. Install Git and Docker using
trusted OS/Docker instructions; Mineval never installs services or changes contexts.

## Standalone release candidates

The repository contains build automation and a verified installer. Public availability
is not assumed: a maintainer must test and publish a versioned release first.
Once a release exists, download `scripts/install.sh` from that reviewed release's source
and run `sh install.sh v0.1.0`. It requires curl, sha256sum and GitHub CLI `gh` for
cryptographic GitHub artifact provenance verification. No Python is needed. Downloads
are versioned; verification fails closed. The default destination is `~/.local/bin`.
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

Setup is noninteractive and saves host-only paths in `$XDG_CONFIG_HOME/mineval/config.json`
(default `~/.config/mineval/config.json`, mode 0600). It reports missing requirements
and does not pull images. Re-run with explicit options to update. Existing stores
are not moved. `MINEVAL_DATA` and explicit `--data` override saved data selection;
without setup the legacy relative `mineval-data` default remains.

See [historical workflows](historical-workflows.md) for evaluation configurations and
end-to-end CLI requests. Default admission budgets reserve 2 CPUs and 2 GiB per job,
plus 512 MiB headroom, increased for declared container limits. `resources` in historical
settings accepts `cpus_per_job`, `memory_mb_per_job`, `reserve_memory_mb`. Stage, total
and per-model concurrency limits still apply. Insufficient resources leave work queued;
run the worker again when capacity is available. Provider limits are operator configured.
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
