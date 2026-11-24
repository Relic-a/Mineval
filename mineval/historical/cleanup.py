"""Task-preserving cleanup artifacts; these are producer claims, not approval."""
import os
import shutil
from mineval.core import processes as subprocess
from uuid import uuid4

from mineval.historical.repository import restore_history
from mineval.core.artifacts import copy_tree, digest, inventory, pack, read_json, relative, write_json

INSTRUCTIONS = """You are preparing the historical base, not solving its task.
Change as little as necessary for reproducible setup. If verify.json contains only
a bootstrap-runtime command, replace it with meaningful repository setup/build/smoke
or tests commands before declaring ready-for-review; the bootstrap is not task verification. Never fix or partially fix
the target behavior, simplify requirements, remove relevant failing tests, or add
meaningless tests. Do not inspect this project's future solution or verification.
Use `mineval verify` for a fresh, consistent sandbox copy of the current workspace
and /mineval-control/verify.json; `mineval verify report` reads its latest report.
Wait for verification to finish, including polling a still-running shell tool; a
pending verification is not evidence. If a required check runs out of memory,
retry with an appropriate explicit limits.memory_mb in verify.json and record the
requirement; do not treat an OOM as a task failure. Install dependencies through verification
commands, not in the cleaned source tree. Keep node_modules and download caches
out of the cleaned tree. Images labeled io.mineval.npm-cache=v1 receive a private
npm cache at /mineval-npm-cache; preserve NPM_CONFIG_CACHE and do not override it.
Images opting into io.mineval.dependency-cache also provide task-private pip,
Go and Cargo inputs. Preserve PIP_FIND_LINKS, GOMODCACHE and CARGO_HOME. Use native
package managers to install into fresh task environments. Report uncached sources
and build dependencies in limitations; never assume every download is cached.
The broker has only these capabilities, no host Docker API. Expected base test
failures are allowed. Annotate a tests command's expected_failure with its reason;
this is a producer claim, not proof that a failure is legitimate.
Record /mineval-control/cleanup-report.json with disposition ready-for-review,
needs-review, or reject; summary; objective_preserved (boolean); changes (one
{path, reason} per workspace path differing from the original base). Paths must be
relative to the workspace. Do not list /mineval-control/verify.json or other control
files in changes: they are outside the workspace and recorded separately. Use an
empty changes list if no workspace files changed. Include environment with
runtime (string), dependencies, services, limitations (lists of strings). Pin
dependencies where possible, describe service/data/credential requirements and
unverified prerequisites. Choose needs-review or reject when essential setup is
unsafe/unavailable or would change the challenge. Ready-for-review is not approval.
No transcripts. The host records a tree archive, patch and command diagnostics.
"""


def validate_report(report, changed):
    if (not isinstance(report, dict) or
            set(report) != {'disposition', 'summary', 'objective_preserved', 'changes', 'environment'} or
            report['disposition'] not in ('ready-for-review', 'needs-review', 'reject') or
            not isinstance(report['summary'], str) or not report['summary'].strip() or
            not isinstance(report['objective_preserved'], bool)):
        raise ValueError('Invalid cleanup report')
    reasons = {}
    if not isinstance(report['changes'], list):
        raise ValueError('Cleanup changes must be a list')
    for change in report['changes']:
        if (not isinstance(change, dict) or set(change) != {'path', 'reason'} or
                not isinstance(change['reason'], str) or not change['reason'].strip()):
            raise ValueError('Each cleanup change requires a path and reason')
        if isinstance(change['path'], str) and change['path'].startswith('/mineval-control/'):
            raise ValueError('Cleanup changes must list workspace-relative paths, not control files')
        path = str(relative(change['path']))
        if path in reasons:
            raise ValueError('Duplicate cleanup change')
        reasons[path] = change['reason']
    if set(reasons) != set(changed):
        raise ValueError('Cleanup report must explain exactly every changed path: ' + repr(changed))
    env = report['environment']
    if (not isinstance(env, dict) or set(env) != {'runtime', 'dependencies', 'services', 'limitations'} or
            not isinstance(env['runtime'], str) or not env['runtime'].strip() or
            any(not isinstance(env[key], list) or any(not isinstance(x, str) or not x.strip() for x in env[key])
                for key in ('dependencies', 'services', 'limitations'))):
        raise ValueError('Record runtime, dependencies, services and limitations')
    if report['disposition'] == 'ready-for-review' and not report['objective_preserved']:
        raise ValueError('Task-altering cleanup requires review or rejection')
    return report


def save_attempt(store, task_id, clean, control, result, *, image_id, diagnostic_ids,
                 producer, input_hashes, instructions_sha256):
    """Called under the task lock, after the writer container has been removed."""
    task, draft = store.task(task_id), store.draft(task_id)
    before = read_json(task / 'base-inventory.json')
    if inventory(task / 'private/base') != before:
        raise ValueError('Stored base provenance was modified')
    current = inventory(clean)
    changed = sorted(p for p in before.keys() | current.keys() if before.get(p) != current.get(p))
    report = None
    issues = []
    try:
        report = validate_report(read_json(control / 'cleanup-report.json'), changed)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        issues.append(str(exc))
    from mineval.historical.verification import load_config
    try:
        config = load_config(control / 'verify.json')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        issues.append('Invalid output diagnostic configuration: ' + str(exc))
        config = load_config(draft / 'control/verify.json')
    latest = task / 'verify-latest.json'
    diagnostic = read_json(latest) if latest.exists() else None
    if (not diagnostic or diagnostic['diagnostic_id'] not in diagnostic_ids or
            diagnostic.get('input_hashes') != current or diagnostic.get('configuration') != config or
            diagnostic.get('status') not in ('completed', 'checks_failed')):
        issues.append('Run a current fresh diagnostic without environment failures')
    if result.exit_code != 0 or result.timed_out or result.oom_killed:
        issues.append('Cleanup process did not complete successfully')
    from mineval.core.devlog import event
    event('cleanup.assessed', level='warning' if issues else 'info',
          exit_code=result.exit_code, diagnostic_count=len(diagnostic_ids), changed_count=len(changed),
          sensitive={'issues': issues})
    disposition = report['disposition'] if report else 'needs-review'
    if issues and disposition == 'ready-for-review':
        disposition = 'needs-review'
    attempt_id = uuid4().hex
    staged = clean.parent / 'attempt'
    staged.mkdir()
    pack(clean, staged / 'workspace.tar.gz')
    write_json(staged / 'tree-hashes.json', current)
    write_json(staged / 'verify-config.json', config)
    with (staged / 'cleanup-changes.patch').open('wb') as out:
        diff = subprocess.run(['git', 'diff', '--no-index', '--no-ext-diff', '--binary',
                               '--src-prefix=base/', '--dst-prefix=clean/',
                               str(task / 'private/base'), str(clean)], stdout=out,
                              stderr=subprocess.PIPE, timeout=120)
        if diff.returncode not in (0, 1):
            raise ValueError('Cannot record cleanup patch')
    record = {'schema_version': 1, 'cleanup_id': attempt_id, 'disposition': disposition,
              'task_id': task_id, 'base_commit': read_json(task / 'metadata.json')['base_commit'],
              'producer': producer, 'input_hashes': input_hashes,
              'instructions_sha256': instructions_sha256,
              'report': report, 'issues': issues, 'changed_paths': changed,
              'image_id': image_id, 'diagnostic_ids': diagnostic_ids,
              'exit_code': result.exit_code, 'timed_out': result.timed_out,
              'oom_killed': result.oom_killed, 'approval': 'pending-independent-review'}
    write_json(staged / 'cleanup-report.json', record)
    write_json(staged / 'artifact-hashes.json', inventory(staged))
    attempts = task / 'cleanups'
    attempts.mkdir(exist_ok=True)
    os.rename(staged, attempts / attempt_id)
    for path in (attempts / attempt_id).iterdir():
        path.chmod(0o444)
    # The lock excludes host operations. Publish only validated tree/config, never
    # arbitrary control files or agent Git metadata. Failed attempts remain reviewable.
    replacement = clean.parent / 'replacement'
    copy_tree(clean, replacement)
    old = clean.parent / 'old-draft'
    os.rename(draft / 'workspace', old)
    try:
        os.rename(replacement, draft / 'workspace')
    except BaseException:
        os.rename(old, draft / 'workspace')
        raise
    write_json(draft / 'control/verify.json', config)
    write_json(task / 'cleanup-latest.json', record)
    return record
