"""Host-only first-run configuration. No credentials are copied into settings."""
import json
import os
from pathlib import Path
import platform
import shutil
import tempfile
from .artifacts import write_json


def config_path():
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'mineval/config.json'


def load():
    path = config_path()
    value = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError('Configuration must be an object')
    return value


def diagnose(sandbox, settings):
    checks = {'linux': platform.system() == 'Linux', 'git': bool(shutil.which('git')),
              'docker_cli': bool(shutil.which('docker'))}
    messages = []
    from .resources import capacity
    cpu, memory = capacity()
    if cpu < 2 or memory < 2560:
        messages.append('Low available resources: preparation will attempt one job at a time; additional workflow admission may leave jobs queued. Free capacity or configure smaller container limits/resource budgets.')
    data = settings.get('data')
    checks['data_directory'] = bool(data and Path(data).is_dir() and os.access(data, os.W_OK))
    if not checks['data_directory']:
        messages.append('Run mineval setup to select a writable persistent data directory.')
    try:
        sandbox.check()
        checks['rootless_docker_controllers'] = True
    except Exception as exc:
        checks['rootless_docker_controllers'] = False
        messages.append(str(exc))
    image = settings.get('image')
    checks['preparation_image'] = False
    if image:
        try:
            sandbox.image(image)
            from .sandbox import SandboxSpec, Limits
            with tempfile.TemporaryDirectory(prefix='mineval-runtime-check-') as tmp:
                inputs = Path(tmp)
                shutil.copyfile(Path(__file__).with_name('codex_runner.py'), inputs / 'codex_runner.py')
                result = sandbox.run(SandboxSpec(image, ('/bin/sh', '-c',
                    'git --version && exec env -i PATH="$PATH" HOME=/tmp CODEX_HOME=/tmp/codex '
                    'python3 /mineval-input/codex_runner.py --check-runtime'),
                    Limits(timeout_seconds=30), inputs=inputs, network='none'))
            checks['preparation_image'] = result.exit_code == 0
            if not checks['preparation_image']:
                messages.append('The preparation image needs runnable Python, Git, Codex and '
                                'codex-code-mode-host (beside Codex or on PATH), including runtime '
                                'dependencies. Select or build a complete trusted image; existing '
                                'drafts also retain their image in control/verify.json.')
        except Exception:
            messages.append('Build/load the trusted preparation image; see docs/installation.md.')
    else:
        messages.append('Run setup --image with a preloaded trusted preparation image.')
    auth = settings.get('auth_file')
    checks['model_auth'] = False
    if auth:
        try:
            from .agents import provision_auth
            with tempfile.TemporaryDirectory() as tmp:
                provision_auth(auth, Path(tmp) / 'auth.json')
            checks['model_auth'] = True
        except (ValueError, OSError, TypeError):
            messages.append('Invalid model-only authentication file; reauthenticate with your model provider.')
    if not checks['model_auth']:
        messages.append('Run setup --auth-file with a model-only Codex auth.json; credentials stay in that file.')
    messages.append('Credential structure is checked locally; provider validity and project toolchains are tested when jobs run.')
    return {'ready': all(checks.values()), 'checks': checks, 'messages': messages}


def setup(args, sandbox):
    settings = load()
    settings.update(data=str(Path(args.data).expanduser().resolve()))
    for name in ('context', 'image', 'auth_file'):
        value = getattr(args, name, None)
        if value:
            settings[name] = str(Path(value).expanduser().resolve(strict=True)) if name == 'auth_file' else value
    Path(settings['data']).mkdir(parents=True, exist_ok=True, mode=0o700)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json(path, settings)
    path.chmod(0o600)
    return diagnose(sandbox, settings) | {'configuration': str(path)}
