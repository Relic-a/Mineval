"""Fresh-environment diagnostics, deliberately unrelated to private extraction."""
from contextlib import nullcontext
import math
import re
from pathlib import Path
from mineval.core import processes as subprocess
import tempfile
from uuid import uuid4

from mineval.core.artifacts import copy_tree, copy_worktree, read_json, write_json
from mineval.core.sandbox import DockerSandbox, Limits, SandboxError, SandboxSpec


def load_config(path, *, evaluation=False):
    config = read_json(path)
    if set(config) - {'image', 'network', 'workdir', 'limits', 'commands', 'grading_type', 'service', 'services', 'script', 'required_tests', 'agent_network'}:
        raise ValueError('Unknown verification configuration fields')
    if 'grading_type' in config and (not evaluation or config['grading_type'] not in ('isolated-argv-v1', 'isolated-http-v1', 'native-script-v1')):
        raise ValueError('Unsupported grading type')
    if config.get('grading_type') == 'isolated-http-v1':
        service = config.get('service')
        if (not isinstance(service, dict) or set(service) != {'argv', 'port'} or
                type(service['port']) is not int or not 1 <= service['port'] <= 65535):
            raise ValueError('HTTP grading requires a service argv and localhost port')
        SandboxSpec(config['image'], service['argv'])
        if config.get('network', 'none') != 'none':
            raise ValueError('HTTP grading requires a network-isolated candidate')
    elif 'service' in config:
        raise ValueError('Service is supported only for isolated HTTP grading')
    limits = Limits(**config.get('limits', {}))
    SandboxSpec(config['image'], ('/bin/sh',), limits, network=config.get('network', 'bridge'), workdir=config.get('workdir', '/workspace'))
    from mineval.core.service_dependencies import validate as validate_services
    validate_services(config.get('services', ()))
    if config.get('agent_network', 'bridge') not in ('none', 'bridge'):
        raise ValueError('agent_network must be none or bridge')
    if config.get('grading_type') == 'native-script-v1':
        from mineval.historical.native_grading import validate_config
        validate_config(config)
        if config.get('commands', []):
            raise ValueError('Native grading runs only the frozen script')
        config.setdefault('commands', [])
        config.setdefault('network', 'none')
        return config
    if {'script', 'required_tests'} & config.keys():
        raise ValueError('Script fields require native-script-v1')
    commands = config.get('commands')
    if not isinstance(commands, list) or not 1 <= len(commands) <= 50:
        raise ValueError('Declare between 1 and 50 diagnostic commands')
    names = set()
    for step in commands:
        allowed = {'name', 'kind', 'argv', 'timeout_seconds', 'expected_failure'}
        if evaluation:
            allowed |= {'failure_exit_codes', 'inconclusive_exit_codes'}
        if set(step) - allowed or step.get('kind') not in ('setup', 'build', 'tests', 'smoke'):
            raise ValueError('Invalid command fields or kind')
        if 'expected_failure' in step and (step['kind'] != 'tests' or
                not isinstance(step['expected_failure'], str) or not step['expected_failure'].strip()):
            raise ValueError('Expected failures require a tests command and a reason')
        if evaluation:
            failure = step.get('failure_exit_codes', [1])
            uncertain = step.get('inconclusive_exit_codes', [2])
            for field in ('failure_exit_codes', 'inconclusive_exit_codes'):
                if field in step and (step['kind'] != 'tests' or not isinstance(step[field], list)
                        or any(type(code) is not int or not 1 <= code < 125 for code in step[field])
                        or len(set(step[field])) != len(step[field])):
                    raise ValueError('Test outcome exit codes must be distinct integers between 1 and 124')
            if set(failure) & set(uncertain):
                raise ValueError('Failure and inconclusive exit codes must not overlap')
        name = step.get('name')
        if not isinstance(name, str) or not name or name in names:
            raise ValueError('Command names must be nonempty and unique')
        names.add(name)
        SandboxSpec(config['image'], step['argv'])
        timeout = step.get('timeout_seconds', limits.timeout_seconds)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= limits.timeout_seconds:
            raise ValueError('Step timeout must be positive and within the sandbox deadline')
    if config.get('grading_type') == 'isolated-argv-v1' and sum(c['kind'] == 'tests' for c in commands) != 1:
        raise ValueError('isolated-argv-v1 requires exactly one tests command')
    if config.get('grading_type') == 'isolated-http-v1':
        if sum(c['kind'] == 'tests' for c in commands) != 1 or commands[-1]['kind'] != 'tests':
            raise ValueError('HTTP grading requires one final tests marker')
    return config


def check_project_config(project, grading):
    for field, default in (('image', None), ('workdir', '/workspace'), ('services', [])):
        if project.get(field, default) != grading.get(field, default):
            raise ValueError(f'Preparation and native grading must use the same project {field}')


def pinned_image(config, diagnostic):
    image = diagnostic.get('image_id', '')
    if (diagnostic.get('configuration') != config or not re.fullmatch(r'sha256:[0-9a-f]{64}', image)
            or diagnostic.get('status') not in ('completed', 'checks_failed')):
        raise ValueError('Native grading requires a current diagnostic with a resolved immutable project image')
    return image


def diagnose(store, task_id, *, sandbox=None, agent=None, already_locked=False,
             workspace=None, config_path=None, persist=True, project_image=None):
    """Pause the owning prep container only long enough to copy its current draft."""
    runner = sandbox or DockerSandbox()
    store.configure_runner(runner, task_id)
    task, draft = store.task(task_id), store.draft(task_id)
    report = {'diagnostic_id': uuid4().hex, 'status': 'environment_error', 'commands': [],
              'warnings': ['Nonzero tests may be the historical task; this report does not accept or reject it.']}
    with (nullcontext() if already_locked else store.operation(task_id)), tempfile.TemporaryDirectory(dir=store.root) as temp:
        try:
            if agent:
                agent.pause()
            try:
                config = load_config(config_path or draft / 'control' / 'verify.json')
                copied = copy_worktree(workspace, Path(temp) / 'workspace') if workspace else copy_tree(draft / 'workspace', Path(temp) / 'workspace')
            finally:
                if agent:
                    agent.unpause()
            report['input_hashes'] = copied
            report['configuration'] = config
            from mineval.historical.repository import restore_history
            restore_history(store, task_id, Path(temp) / 'workspace')
            limits = Limits(**config.get('limits', {}))
            image = config['image']
            if project_image is not None:
                configured, resolved = project_image
                if configured != image:
                    raise ValueError('Cleanup diagnostics must use the same project image as their preparation container')
                image = resolved
            spec = SandboxSpec(image, ('/bin/sh', '-c', 'while :; do sleep 1; done'), limits,
                               workspace=Path(temp) / 'workspace', network=config.get('network', 'bridge'),
                               workdir=config.get('workdir', '/workspace'), services=config.get('services', ()))
            with runner.container(spec) as container:
                container.start()
                report.update(container_id=container.id, image_id=container.image_id, status='completed')
                service_images = getattr(container, 'service_images', None)
                if isinstance(service_images, dict):
                    report['service_images'] = service_images
                cache_stats = getattr(container, 'cache_stats', None)
                if isinstance(cache_stats, dict):
                    report['download_cache'] = cache_stats
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
                    interpretation = ('environment_failure' if status in ('timeout', 'oom', 'environment_error', 'command_error')
                                      or (status == 'failed' and command['kind'] in ('setup', 'build')) else
                                      'expected_task_failure' if status == 'failed' and command.get('expected_failure') else
                                      'behavior_failure' if status == 'failed' else 'passed')
                    report['commands'].append({'name': command['name'], 'kind': command['kind'], 'status': status,
                                               'interpretation': interpretation,
                                               'expected_failure': command.get('expected_failure'), **result})
                    if result['logs_truncated']:
                        report['warnings'].append(f"Output truncated for command {command['name']}")
                    stopped = interpretation == 'environment_failure' or not container.inspect()['Running']
                if any(x['status'] != 'passed' for x in report['commands']):
                    report['status'] = ('environment_error' if any(x.get('interpretation') == 'environment_failure'
                                                                  or x['status'] == 'skipped' for x in report['commands']) else 'checks_failed')
        except (SandboxError, ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            from mineval.core.devlog import failure
            failure("verification.error", exc)
            report['status'] = 'environment_error'
            report['error'] = str(exc)
            report['cleanup_notes'] = getattr(exc, '__notes__', [])
        if not persist:
            return report
        reports = task / 'diagnostics'
        write_json(reports / (report['diagnostic_id'] + '.json'), report)
        from mineval.core.artifacts import digest
        write_json(reports / (report['diagnostic_id'] + '.sha256.json'), {'sha256': digest(reports / (report['diagnostic_id'] + '.json'))})
        write_json(task / 'verify-latest.json', report)
        return report
