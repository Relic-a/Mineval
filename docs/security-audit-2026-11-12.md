# Mineval security review — 2026-11-12

Reviewed checkout: `98dfb58b17efa47a1ed4953cad4b9b5c1703e577`.

**Conclusion:** “Credentials, remote execution and private tests are secure” is too broad. Mineval has useful host isolation and explicit handling of secrets, but it does not protect credentials from the coding agent or private tests from adversarial candidate code. It also accepts a successful test-process exit without independently establishing that assertions executed.

This was a read-only implementation and runtime review, apart from temporary synthetic probes and this report. No production settings, credentials, benchmark records or verdicts were changed. No model inference, image builds, downloads, external publication or outbound exfiltration probes were performed.

## Evidence reviewed

- Traced the benchmark, preparation, cleanup broker, extraction/review, evaluation, qualitative memory, sandbox, artifact, dependency-cache, SSH transfer and local observation/Arena boundaries.
- Inspected local diagnostic metadata and content using scripts that emitted only counts, paths and boolean matches, never credentials or raw private test content.
- Inspected the already configured VPS over SSH with strict host-key checking. Its installed `agents.py` and `sandbox.py` hashes matched this checkout. Other remote modules were not individually compared.
- At the initial scan, local diagnostics contained 961 session directories, 2,884 files and 222,942,894 bytes. Retained timestamps ranged from November 3 to November 12. All checked log files were mode 0600. Local raw-output records included 25 records with Codex thread-start events and seven with extraction-manifest references. Some sessions are synthetic test runs, so these are not production-run counts.
- VPS diagnostics contained 36 sessions, 108 files and 27,374,346 bytes, dated November 9–12. All checked diagnostic files were mode 0600; worker root was 0700 and its auth file 0600. There were 12 job directories and one retained export. Its empty `coordinator.log` was 0664, protected by the 0700 worker parent.
- Compared the three current credential strings in each host's configured auth file against that host's retained diagnostic bytes: zero exact matches. This does **not** establish that previous, refreshed, encoded or unrelated secrets never appeared. A token-field-name reference in a trace is not evidence of a leaked token value.
- Local task/draft scan covered 3,297 files and 22 archives containing 3,921 regular file entries. No exact current configured credential values were found, and no archive entries named `.env`, `auth.json`, `.npmrc`, `id_rsa` or `id_ed25519` were found. This was not a comprehensive historical secret scan or a VPS artifact scan.
- Existing verification: 5 diagnostic tests plus 95 agent/sandbox/preparation/evaluation/transfer tests completed successfully, with two live tests skipped. Two additional temporary synthetic probes ran against the configured local rootless runtime.

## Prioritized findings

### 1. High — model credentials are readable by the coding agent

**Confirmed by code and a live synthetic probe.** `agents.py:118–134` accepts API keys and access/refresh/ID tokens. `agents.py:219–223` stages the auth file in the agent's input mount and enables bridge networking. `agents.py:55–58` also copies it into `/tmp/codex/auth.json`. `sandbox.py:440` runs the container as UID 0, and `sandbox.py:478–480` mounts inputs read-only. Read-only prevents changes; it does not prevent reads. Mode 0600 also does not separate processes running as the same user.

The probe staged a **dummy** credential through `provision_auth`, used the standard sandbox with networking disabled and successfully read it from `/mineval-input/auth.json`. No real credential was used. The same file is visible to model shell tools and repository install/build scripts running in that container. They can copy it into saved source, print it into logs, or attempt to send it over the allowed network. Forwarding refresh tokens increases the potential lifetime of compromise.

**Fix:** move provider authentication into a trusted process outside the agent execution boundary, ideally behind a narrow inference gateway with short-lived, scoped, budget-limited run credentials. A separate file permission or moving the same secret into an environment variable is insufficient. Until that separation exists, explicitly describe the credential as entrusted to the agent; use dedicated credentials with limited spend and privileges.

README already acknowledges agent credential exfiltration at approximately lines 731–746. This is a documented limitation, not a newly discovered contradiction in that section.

### 2. High — private tests are visible to candidate code during grading

**Confirmed by code and a live synthetic probe.** The coding turn receives the starting tree, but `evaluation.py:115–147` later copies candidate code and private checks into the same execution workspace. Nested read-only mounts protect selected files against replacement; they do not hide contents. Candidate imports, setup scripts and test hooks can read those files. Grading defaults to bridge networking at `evaluation.py:146`.

The synthetic candidate read the contents of its synthetic private check successfully, with networking disabled. Thus the disclosure-to-code path is proven. No attempt was made to transmit anything. When network access is enabled, candidate code has a potential path to send checks to an external endpoint. This does not show that any previous run did so, or that checks were fed back into its original coding-model conversation.

**Fix:** separate dependency preparation from offline grading and default grading to `network='none'`. Isolate the private harness from candidate filesystem/process access where the task permits it, using a narrow behavioral interface. For arbitrary in-process source tests, fully hiding the harness is a substantially harder architecture problem; state that trust assumption explicitly.

### 3. High — a candidate can produce a passing verdict before assertions execute

**Confirmed by a live synthetic probe through `execute_checks`.** `evaluation.py:159–164` classifies exit code zero as passed. The synthetic check imported a deliberately wrong candidate and then asserted the expected result. During import, the candidate read the private check and called `os._exit(0)`. Mineval returned:

```json
{"verdict":"pass","exit_code":0,"candidate_can_read_private_test":true}
```

The assertion after the import never ran. The probe used a trusted, direct `python3 private_check.py` command, read-only private check mounts and the normal evaluation path. It required no test-file modification. Framework-specific exploitation and prevalence in existing runs were not established.

**Fix:** add a trusted harness that verifies expected test discovery, execution and completion, and treats missing completion/results as an error. Keep verdict authority outside the candidate process. A completion file writable by candidate code, or trusting a candidate-controlled test report alone, does not provide that separation. Add this early-success-exit case as an adversarial regression fixture.

### 4. Medium — raw traces and outputs are retained without redaction or global expiry

**Confirmed in code and retained logs.** `sandbox.py:506–512` collects raw container output regardless of `retain_logs=False`. That flag suppresses normal result output; it does not suppress sensitive diagnostics. `codex_runner.py:54–58` streams raw Codex events, and `processes.py:19–28` / `59–62` records arguments and captured outputs. `devlog.py:56–71` serializes arbitrary sensitive details without redaction. Existing tests intentionally prove that a synthetic secret enters `sensitive.jsonl`.

Permissions are good: checked diagnostics are 0600 in restricted session directories. Sensitive diagnostics are outside task artifacts and are not mounted into agents. However, a tool that echoes a credential will persist it there. Raw private-evidence references and Codex event streams are present in real retained diagnostics. `devlog.py:44` rotates each stream, but old session directories have no automatic global age/size limit. Local logs already occupied about 223 MB at inspection.

There is a second retention path: `evaluation.py:173` persists command stdout/stderr in evaluation evidence, and `verification.py:100–127` persists diagnostic output. Those outputs should be treated as sensitive even when separate from raw agent transcripts.

**Fix:** make raw trace capture explicitly configurable and disabled by default for routine operation; redact registered credentials and recognized secret patterns before every durable output sink; add global retention and deletion controls. Redaction is defense in depth and cannot guarantee detection of transformed secrets. Classify evaluation reports and exported evidence as private. Avoid promising that any stream is universally safe to share solely because it is named dev/prod.

### 5. Medium — writable bind mounts can exhaust host disk or inodes

**Confirmed configuration gap; no destructive probe performed.** `sandbox.py:474–480` applies `workspace_mb` only to tmpfs workspaces. Normal benchmark/evaluation workspaces and control directories are host bind mounts with no per-run storage quota. Cache working directories are writable binds too. `artifacts.py` byte/file limits apply while inspecting/exporting files, after the workload has already written them.

A faulty or adversarial candidate can fill the local or VPS filesystem before collection, affecting other jobs, logs and databases. CPU, memory, PID limits and timeouts do not impose a disk/inode quota. README already documents the bind-workspace quota limitation.

**Fix:** enforce byte and inode quotas on every writable per-run volume using suitable filesystem/project quotas or dedicated bounded volumes. Include control and cache copies. Add free-space admission and emergency cancellation as supplementary controls, plus global log/job retention.

### 6. Medium — online solution lookup remains a policy, not isolation

**Confirmed design limitation.** `agents.py:13–20` explicitly says future-solution lookup is prohibited by trust policy rather than technically prevented. Coding runs use bridge networking. Sanitizing local Git history does not prevent a model from finding public future commits or PRs online. `historical_solution_seen` is a self-report, not independent verification.

**Fix:** offer a strict benchmark mode with offline coding after dependency preparation, or a controlled egress gateway that separates inference access from general web access. Report network mode and residual contamination risk alongside results. No claim is made here that a retained run actually looked up its solution.

## Private tests: exactly who sees them?

| Boundary | Finding |
| --- | --- |
| Original benchmark coding session | Code path exports the starting tree and reconstructed pre-base history, plus task statement; no direct private-test mount found. |
| Saved candidate during grading | Can read private test files in the shared grading workspace; synthetically demonstrated. |
| Extraction/review models | Deliberately receive future trees, diffs and private evidence in fresh containers (`review.py:240–287`). |
| Independent evaluator model | Deliberately receives verification contents and baseline results (`evaluation.py:191–213`). |
| Qualitative review model | Deliberately receives bounded private test contents (`model_review.py:63–80`). |
| Human dashboard and trusted VPS account | Deliberately have access to private evidence. These are trusted principals, not public views. |

Separate sessions prevent automatic conversation reuse; they do not mean private tests are never sent to a model provider. Provider retention, training policy and pre-existing model knowledge were not verified. Candidate code and evaluator proposals also create a prompt-injection/semantic-grading risk: schema and evidence citations validate structure/provenance, not the truth or behavioral equivalence of an adaptation. The code already acknowledges that limitation at `evaluation.py:356–357`. No successful evaluator prompt-injection attack was demonstrated in this audit.

## Controls that are working

- Sandbox checks fail closed for rootless Docker and required cgroup controllers; containers use read-only roots, dropped capabilities, no-new-privileges, CPU/memory/PID limits and bounded Docker logs.
- Normal sandbox mounts contain disposable task directories, not the host home, Docker socket or entire private store. Agent Git metadata is discarded before exporting output.
- Historical Git reconstruction validates that stored objects belong to the base closure and strips source remotes/configuration. The existing future/unreachable-object exclusion tests passed.
- SSH uses argv, batch mode and strict host-key checking. Worker requests carry credentials over stdin rather than process arguments. Remote errors return exception categories, not raw request/credential contents. Worker root/auth permissions were verified on the configured VPS.
- Transfer checksums, explicit path validation, link/special-file rejection and staged publication protect against several corruption and archive traversal cases. Hash receipts establish integrity under a trusted owner; they do not protect evidence from an attacker controlling that owner account.
- Dependency caches validate allowed sources and payload hashes, use private per-run copies, and do not generally import arbitrary candidate cache output into the shared dependency store.
- Observation/Arena servers bind loopback and use unpredictable URL prefixes, escaping/CSP and mutation-origin checks. Keep these human URLs outside agent inputs; full browser penetration testing was outside scope.

## Recommended order

1. Correct the blanket assurance and make the threat model explicit: trusted operator/VPS/image, untrusted candidate code, and whether malicious benchmark agents are in scope.
2. Remove long-lived provider credentials from the agent execution boundary.
3. Make grading offline and add trusted completion checks that reject early successful exits.
4. Add raw-log opt-in, redaction, output classification and global retention controls.
5. Enforce per-run disk/inode quotas, then add strict/offline coding mode.
6. Add secret scanning for imported source, pre-base Git history and exported workspaces. Current source/history can contain embedded secrets even when the harness never mounts host credential directories. Do not silently rewrite benchmark history: quarantine/review affected tasks.

The highest-priority demonstrated failures are credential readability and an unexecuted assertion being reported as passed. I found no evidence of a current credential value leaking into the logs inspected, no direct private-test mount into the original coding turn, and no demonstrated host escape. Those bounded observations are not an assurance that every run or deployment is secure.
