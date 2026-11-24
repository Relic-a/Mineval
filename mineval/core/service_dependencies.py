"""Per-sandbox service containers with private DNS and disposable state."""
from contextlib import contextmanager, ExitStack
from subprocess import TimeoutExpired
import time
from uuid import uuid4

from mineval.core.sandbox_types import Limits, SandboxError, validate_services as validate

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
