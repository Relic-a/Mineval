"""Fresh-environment diagnostics, deliberately unrelated to private extraction."""
from contextlib import nullcontext
import math
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4

from .artifacts import copy_tree, read_json, write_json
from .sandbox import DockerSandbox, Limits, SandboxError, SandboxSpec


def load_config(path):
    config = read_json(path)
    if set(config) - {'image', 'network', 'workdir', 'limits', 'commands'}:
        raise ValueError('Unknown verification configuration fields')
    limits = Limits(**config.get('limits', {}))
    SandboxSpec(config['image'], ('/bin/sh',), limits, network=config.get('network', 'bridge'), workdir=config.get('workdir', '/workspace'))
    commands = config.get('commands')
    if not isinstance(commands, list) or not 1 <= len(commands) <= 50:
        raise ValueError('Declare between 1 and 50 diagnostic commands')
    names = set()
    for step in commands:
        if set(step) - {'name', 'kind', 'argv', 'timeout_seconds'} or step.get('kind') not in ('setup', 'build', 'tests', 'smoke'):
            raise ValueError('Invalid command fields or kind')
        name = step.get('name')
        if not isinstance(name, str) or not name or name in names:
            raise ValueError('Command names must be nonempty and unique')
        names.add(name)
        SandboxSpec(config['image'], step['argv'])
        timeout = step.get('timeout_seconds', limits.timeout_seconds)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= limits.timeout_seconds:
            raise ValueError('Step timeout must be positive and within the sandbox deadline')
    return config


def diagnose(store, task_id, *, sandbox=None, agent=None, already_locked=False):
    """Pause the owning prep container only long enough to copy its current draft."""
    runner = sandbox or DockerSandbox()
    task, draft = store.task(task_id), store.draft(task_id)
    report = {'diagnostic_id': uuid4().hex, 'status': 'environment_error', 'commands': [],
              'warnings': ['Nonzero tests may be the historical task; this report does not accept or reject it.']}
    with (nullcontext() if already_locked else store.operation(task_id)), tempfile.TemporaryDirectory(dir=store.root) as temp:
        try:
            if agent:
                agent.pause()
            try:
                config = load_config(draft / 'control' / 'verify.json')
                copied = copy_tree(draft / 'workspace', Path(temp) / 'workspace')
            finally:
                if agent:
                    agent.unpause()
            report['input_hashes'] = copied
            report['configuration'] = config
            limits = Limits(**config.get('limits', {}))
            spec = SandboxSpec(config['image'], ('/bin/sh', '-c', 'while :; do sleep 1; done'), limits,
                               workspace=Path(temp) / 'workspace', network=config.get('network', 'bridge'),
                               workdir=config.get('workdir', '/workspace'))
            with runner.container(spec) as container:
                container.start()
                report.update(container_id=container.id, image_id=container.image_id, status='completed')
                import time
                deadline = time.monotonic() + limits.timeout_seconds
                stopped = False
                for command in config['commands']:
                    if stopped:
                        report['commands'].append({'name': command['name'], 'status': 'skipped'})
                        continue
                    budget = min(command.get('timeout_seconds', limits.timeout_seconds), deadline - time.monotonic())
                    if budget <= 0:
                        stopped = True
                        report['commands'].append({'name': command['name'], 'status': 'skipped', 'reason': 'sandbox deadline exhausted'})
                        continue
                    result = container.exec(command['argv'], timeout=budget, workdir=spec.workdir)
                    code = result['exit_code']
                    status = ('timeout' if result['timed_out'] else 'oom' if result['oom_killed'] else
                              'environment_error' if code == 125 else 'command_error' if code in (126, 127) else 'passed' if code == 0 else 'failed')
                    report['commands'].append({'name': command['name'], 'kind': command['kind'], 'status': status, **result})
                    if result['logs_truncated']:
                        report['warnings'].append(f"Output truncated for command {command['name']}")
                    stopped = code == 125 or result['timed_out'] or result['oom_killed'] or not container.inspect()['Running']
                if any(x['status'] != 'passed' for x in report['commands']):
                    report['status'] = ('environment_error' if any(x['status'] == 'environment_error' for x in report['commands']) else 'checks_failed')
        except (SandboxError, ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            report['status'] = 'environment_error'
            report['error'] = str(exc)
            report['cleanup_notes'] = getattr(exc, '__notes__', [])
        reports = task / 'diagnostics'
        write_json(reports / (report['diagnostic_id'] + '.json'), report)
        from .artifacts import digest
        write_json(reports / (report['diagnostic_id'] + '.sha256.json'), {'sha256': digest(reports / (report['diagnostic_id'] + '.json'))})
        write_json(task / 'verify-latest.json', report)
        return report
