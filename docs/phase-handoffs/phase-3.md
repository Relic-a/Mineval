# Phase 3 — isolated execution and dedicated adapters

- **Implemented:** `mineval/history.py` reconstructs fresh Git from precisely
  base-reachable objects, including merge ancestry. New Git intake saves private
  `history.pack`; new frozen revisions publish `base-pack-v1`. Materialization
  preserves preparation edits and attaches validated historical Git. Legacy/raw
  tree-only revisions remain readable. `mineval/agents.py` and `agent-run` add fake
  and Codex adapters, disposable workspace/input/control copies, bridge networking,
  structured completion/error/timeout/OOM handling and existing rootless lifecycle
  cleanup. No host repo/home/socket/private-store mounts or host agent execution.
  Codex uses stdin, ephemeral state, no user config/rules, structured final output,
  explicit model (default `gpt-6-luna`) and optional copied model-only auth.
  Agent logging is disabled; only a tree archive and brief report survive. README,
  foundation docs and `examples/Dockerfile.agent` describe setup and limitations.
- **Decisions made during this phase:** Keep the tree codec plus separately hashed
  base pack rather than rewriting archives/old revisions. Reject shallow sources,
  unsupported filesystem entries and untrusted Git metadata. Validate all stored
  objects, not just refs. Discard final agent Git metadata without executing host
  Git on it. This is an execution primitive, not Phase 6 orchestration. Auth is
  explicit, allowlisted, temporary and readable by the agent. Images must be trusted
  and provide project runtimes explicitly. Solution lookup remains a trust policy;
  the saved declaration is self-reported. No automatic login/config/binary mounts.
- **Checks performed:** On 2026-10-24, installed rootless Docker packages were
  available; enabled the user service and created `mineval-rootless` without changing
  the default context. `doctor` passed on Docker 29.9.2 with rootless, cgroup v2 and
  systemd resource controls. Full suite with `MINEVAL_TEST_CONTEXT=mineval-rootless`,
  `MINEVAL_TEST_IMAGE=mineval-fixture:phase3`, and
  `MINEVAL_AGENT_TEST_IMAGE=mineval-fixture:phase3`: **60 tests passed, none skipped**.
  Live checks cover readonly root, capabilities, limits, fresh diagnostics, bind-copy
  protection, agent input mounts, disabled agent logs, success/error/timeout and
  container removal. No managed test containers remained afterward. This exposed a
  Docker 29 local-log incompatibility: disabled compression for the existing one-file
  bounded diagnostic logs. Agent logs remain fully disabled. Corrected the live
  assertion to inspect tmpfs separately from bind mounts.

  Two short, explicitly authorized `gpt-6-luna` smoke calls used a synthetic historical
  fixture and temporary copied Codex authentication only. First call authenticated
  but could not start command tools; no marker was produced. Added Bash and the
  matching code-mode companion to the native image. Second call completed in 21.0s,
  producing the exact marker contents and reporting successful git log/blame. Host
  checks independently verified the marker, no other project file changes, unchanged
  source fixture and removal of temporary files. No transcripts were saved. Completion
  status means harness termination, not proof of task success; the marker check caught
  the first attempt. Historical-solution declaration remains self-reported.

  Tested image `mineval-agent:phase3-ready` uses Python/Alpine with Git, Bash and copies
  of the installed static Linux x86_64 Codex 0.164.0 plus code-mode companion. Image ID
  `sha256:7346240145527028d5a61cb9435ebeae08e7746c4655c523519ea010e8b07364`.
  Native CLI/version/flags were checked in isolation before model calls. See
  `examples/Dockerfile.agent-native` and README for reproducible inputs. Credentials,
  user config/home and live repositories were never supplied to image builds.
- **Remaining limitations/blockers:** Rootless Docker, live fake execution, subscription
  inference and Luna access are now verified on this machine. Subscription token refresh
  and other platforms/runtime images remain unverified. The original Debian/npm recipe
  was interrupted due to slow/stalling package mirrors; added bounded download retries
  but did not validate that image. An attempted host-network build was setup-only and
  canceled; all agent containers used bridge networking. No need to change the agent
  isolation policy. Old/tree-only imports require complete Git reimport and new
  revisions. Bounded artifacts reject symlinks/submodules; bind workspaces lack a hard
  disk quota. Open-network agents can copy/exfiltrate supplied model credentials;
  review outputs for secrets. Prior generic preparation/extraction commands remain
  unchanged; later phases must wire adapters into their operations. No automated
  cleanup/review/evaluation/benchmark workflow was added.
- **Inputs for next phase:** `run_agent` accepts sanitized materialized workspace,
  task text, new output directory, trusted image, adapter, limits and optional auth.
  It saves `workspace.tar.gz` and `result.json`. `FakeAdapter` supports success/error/
  timeout. `MINEVAL_AGENT_TEST_IMAGE` opts into live fake checks; `MINEVAL_TEST_CONTEXT`
  selects rootless Docker. Draft trees remain Git-free; use the protected history
  pack when creating isolated preparation agent copies. `Store.materialize`
  reconstructs frozen `base-pack-v1` history. Phase 3 stops here; later suite/run/
  evaluation identities are untouched.
