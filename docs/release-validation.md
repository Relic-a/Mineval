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
attested release. Test a fresh Linux rootless Docker host, the starter image and a
paid benchmark with explicit authorization. Verify target glibc compatibility.
Build dependencies are version/hash locked; byte-identical executable reproducibility
has not been established. Authorize release upload and PyPI publication separately.
No packages, public images or releases were published by this work.
