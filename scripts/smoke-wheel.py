"""Exercise an installed wheel outside the checkout, without host settings."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import urllib.request
import venv


wheel = Path(sys.argv[1]).resolve(strict=True)
with tempfile.TemporaryDirectory(prefix='mineval-wheel-smoke-') as tmp:
    root = Path(tmp)
    home = root / 'home'
    home.mkdir()
    env = {'HOME': str(home), 'PATH': os.defpath, 'XDG_CONFIG_HOME': str(home / '.config'),
           'XDG_DATA_HOME': str(home / '.local/share'), 'XDG_STATE_HOME': str(home / '.local/state')}
    venv.create(root / 'venv', with_pip=True)
    binary = root / 'venv/bin/mineval'
    subprocess.run([str(root / 'venv/bin/pip'), 'install', '--no-index', '--no-deps', str(wheel)],
                   cwd=root, env=env, check=True)
    def run(*args, expected=0):
        result = subprocess.run([str(binary), *args], cwd=root, env=env,
                                text=True, capture_output=True, timeout=60)
        if result.returncode != expected:
            raise RuntimeError(f'{args}: exit {result.returncode}: {result.stdout} {result.stderr}')
        return result.stdout

    assert 'doctor' in run('--help')
    setup = json.loads(run('--data', str(root / 'store'), 'setup', expected=125))
    assert not setup['ready'] and (home / '.config/mineval/config.json').is_file()
    doctor = json.loads(run('doctor', expected=125))
    assert not doctor['ready'] and doctor['checks']['data_directory']
    assert json.loads(run('task', 'list')) == []
    process = subprocess.Popen([str(binary), 'observe', '--port', '0'], cwd=root, env=env,
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        if not select.select([process.stdout], [], [], 30)[0]:
            raise RuntimeError('Dashboard did not start within 30 seconds')
        line = process.stdout.readline().strip()
        url = line.split(': ', 1)[1]
        with urllib.request.urlopen(url, timeout=10) as response:
            assert b'Mineval' in response.read()
    finally:
        process.terminate()
        process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()
    print('Wheel installation, first-run diagnostics, task list and dashboard passed.')
