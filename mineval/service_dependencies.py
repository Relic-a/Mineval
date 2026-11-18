"""Per-sandbox service containers with private DNS and disposable state."""
from contextlib import contextmanager, ExitStack
import re
from subprocess import TimeoutExpired
import time
from uuid import uuid4

from .sandbox import Limits, SandboxError, SandboxSpec

PIN = re.compile(r'(?:sha256:|[^\s@]+@sha256:)[0-9a-f]{64}')


def validate(services):
    if not isinstance(services, (list, tuple)) or len(services) > 8:
        raise ValueError('Declare at most eight project services')
    names = set()
    for service in services:
        if not isinstance(service, dict) or set(service) - {
                'name', 'image', 'ready', 'environment', 'argv', 'tmpfs', 'limits'}:
            raise ValueError('Invalid project service fields')
        name = service.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', name) or name in names:
            raise ValueError('Project services require unique DNS names')
        names.add(name)
        image = service.get('image', '')
        if not isinstance(image, str) or not PIN.fullmatch(image):
            raise ValueError(f'Service {name} requires a pinned image digest or ID')
        SandboxSpec(image, service.get('ready', ()))
        if 'argv' in service:
            SandboxSpec(image, service['argv'])
        Limits(**service.get('limits', {}))
        env = service.get('environment', {})
        if not isinstance(env, dict) or any(
                not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or not isinstance(value, str) or '\0' in value
                for key, value in env.items()):
            raise ValueError(f'Invalid environment for service {name}')
        paths = service.get('tmpfs', [])
        if not isinstance(paths, list) or len(paths) > 8 or any(
                not isinstance(path, str) or not re.fullmatch(r'(?:/[A-Za-z0-9_.-]+)+', path)
                or any(part in ('.', '..') for part in path.split('/')) for path in paths):
            raise ValueError(f'Invalid tmpfs paths for service {name}')


@contextmanager
def group(runner, spec):
    cli = runner._cli
    network = 'mineval-' + uuid4().hex
    with ExitStack() as cleanup:
        # Register by name before launch so partial creation also unwinds.
        def remove(*args):
            try:
                cli.call(*args)
            except SandboxError as exc:
                if 'no such' not in str(exc).lower() and 'not found' not in str(exc).lower():
                    raise
        cleanup.callback(remove, 'network', 'rm', network)
        cli.call('network', 'create', '--label', 'io.mineval.managed=true',
                 *(['--internal'] if spec.network == 'none' else []), network)
        resolved = {}
        for service in spec.services:
            name = network + '-' + service['name']
            image = runner.image(service['image'])
            resolved[service['name']] = image['Id']
            limits = Limits(**service.get('limits', {}))
            args = ['create', '--name', name, '--label', 'io.mineval.managed=true',
                    '--pull=never', '--network', network, '--network-alias', service['name'],
                    '--read-only', '--security-opt=no-new-privileges=true', '--init',
                    '--restart=no', '--no-healthcheck', '--memory', f'{limits.memory_mb}m',
                    '--memory-swap', f'{limits.memory_mb}m', '--cpus', str(limits.cpus),
                    '--pids-limit', str(limits.pids), '--log-driver', 'local',
                    '--log-opt', 'max-size=5m', '--log-opt', 'max-file=1', '--log-opt', 'compress=false']
            for key, value in service.get('environment', {}).items():
                args.extend(('--env', f'{key}={value}'))
            paths = dict.fromkeys(['/tmp', *service.get('tmpfs', []),
                                   *((image.get('Config', {}).get('Volumes') or {}).keys())])
            for path in paths:
                args.extend(('--tmpfs', f'{path}:rw,nosuid,nodev,size={limits.tmp_mb}m'))
            cleanup.callback(remove, 'rm', '--force', '--volumes', name)
            cli.call(*args, image['Id'], *service.get('argv', ()))
            cli.call('start', name)
            deadline = time.monotonic() + min(limits.timeout_seconds, spec.limits.timeout_seconds)
            while True:
                if runner.cancel_event is not None and runner.cancel_event.is_set():
                    raise KeyboardInterrupt()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SandboxError(f"Service {service['name']} did not become ready")
                try:
                    cli.call('exec', name, *service['ready'], timeout=min(3, remaining))
                    break
                except (SandboxError, TimeoutExpired):
                    time.sleep(max(0, min(0.25, deadline - time.monotonic())))
        yield network, resolved
