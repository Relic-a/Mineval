# Release validation performed during reliability work

Local tests: `python -m unittest discover -s tests`: **210 tests, 26 skipped, passed**.
Skips cover optional/platform/integration checks; this is not a claim that those ran.
New tests cover interruptions after evidence sealing and before record receipt,
run reuse, distinct evaluation IDs, compaction retry/provenance, resource budgets,
concurrent warm seed isolation, settings, installer upgrade/removal and failed verification.
Installer tests use mocked download/attestation commands; live release authenticity
cannot be acceptance-tested before an authorized attested release exists.

Built a wheel and sdist with `python -m build`, installed the wheel in a separate
virtual environment and ran CLI help outside the repository. Built the one-file
executable using PyInstaller 6.22.0. Initial host Python 3.14 build failed on Debian
because it required GLIBC_2.44. Rebuilding with portable CPython 3.12.16 succeeded.

Executed the binary in an existing `postgres:17` container with no Python, no
network, read-only filesystem and executable temporary tmpfs: CLI help, setup,
doctor and task list worked. Setup/doctor correctly returned 125 with actionable
missing Git/Docker/auth/image diagnostics. This validates Python independence,
not a complete fresh-host rootless Docker installation.

Outside the checkout, isolated config/data smoke verified setup persistence,
doctor, JSON task list and HTTP dashboard response with bundled CSS (9,835 bytes).
The skill passed its structural validator. An independent agent followed CLI help,
setup/doctor and list workflows without the web or paid model calls. Its findings
led to skill installation instructions and stronger local prerequisites checks.
Three live rootless Docker integration tests also passed using preloaded
`mineval-fixture:phase4` on the existing `mineval-rootless` context (isolation,
resource constraints, deadlines and cleanup).
A paid end-to-end historical benchmark using the new starter image was not run.

Synthetic cache workload: 500 integrity-addressed npm artifacts, one warm seed.
Before removing the redundant inventory pass: cold 0.480s, profiled warm 0.483s,
four concurrent warm consumers 1.416s. After: cold 0.490s, profiled warm 0.414s,
four concurrent warm consumers 1.220s. Single runs on this machine; profiling adds
overhead and these are not representative of every project. Source before/after
and destination hashing remain; receipt comparison shares the existing source pass.
No installed Python environment or materialized Git integrity checks were removed.

## Required before publication

Run the workflow from the matching version tag, inspect candidate checksums and
GitHub attestations, and validate installation/upgrade/uninstall against an actual
attested release. Test a fresh Linux rootless Docker host and the starter image; further paid
benchmarks require explicit authorization. Verify target glibc compatibility.
Build dependencies are version/hash locked; byte-identical executable reproducibility
has not been established. Authorize release upload and PyPI publication separately.
No packages, public images or releases were published by this work.

## 0.1.0a2 local installation acceptance

Downloaded the published 0.1.0a1 wheel and sdist with authenticated GitHub CLI and
verified both against the release SHA256SUMS. Installed the wheel into a dedicated
user-local virtual environment, exposed `~/.local/bin/mineval`, and ran outside the
checkout. Saved setup uses the existing `mineval-rootless` context, preloaded
`mineval-agent:phase3-native` image and the explicit model-only Codex authentication
file. Setup and doctor reported ready; model discovery returned the available catalog.
No provider inference was performed.

The documented fake historical demo failed after setup because saved credentials
were passed to non-Codex adapters. Fixed credential defaults to apply only to model
operations; explicit credential arguments still reach validation. Also reproduced
`doctor --data` reporting the saved store's readiness for a missing requested store;
doctor now checks the effective CLI/environment/saved selection.

Built 0.1.0a2 wheel and sdist, upgraded the local installation, and ran the complete
historical demo using the installed package outside the checkout with preloaded
`mineval-fixture:phase4`. Import, cleanup/broker diagnostics, private extraction,
base/future checks, review, freeze, suite run, independent evaluation, retirement,
receipt preservation and eight HTTP routes passed. The deliberately unsolved fake
run correctly received a fail verdict. The installed `observe` command also served
HTTP 200 and shut down cleanly. The corrected doctor returned not-ready for an
explicit nonexistent data directory.

This validates the existing Linux host and preloaded images, not a fresh-host
Docker installation, starter image build or paid-model benchmark. Standalone
publication remains blocked by the existing attestation policy.

The full suite with live historical/rootless and Arena integration enabled passed:
214 tests, 6 skipped (optional toolchains/capture). The installed Arena CLI also
created a brief and generated a ready fixture result with saved authentication present.


## 0.2.0a1 VPS validation

Paid end-to-end workflows completed on an existing VPS for the same two frozen
Gretel tasks with GPT-6 Luna medium and GPT-6.1 Terra medium. Luna received fail
verdicts on both tasks. Terra received a fail on the queue task and an inconclusive
verdict on the search task because the historical test interface could not be
defensibly adapted. These results describe these tasks and configurations, not
general model performance or broad product readiness.

The local benchmark was not rerun in this session. This VPS validation does not
establish fresh-host installation acceptance. Earlier sections describe validation
performed at their stated times and retain their historical limitations.
