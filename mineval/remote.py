"""Authenticated SSH transport to a private Mineval worker."""
import json
import hashlib
import io
import shlex
import zipfile
import re
import subprocess
import tempfile
from pathlib import Path

from .agents import provision_auth
from .artifacts import read_json


class RemoteError(ValueError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class SSHClient:
    def __init__(self, host):
        if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.:-]*', host):
            raise ValueError('VPS must be an SSH user@host')
        self.host = host

    def request(self, request, *, timeout=60):
        try:
            result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                                     '-o', 'ConnectTimeout=10', '--', self.host,
                                     'PYTHONPATH="$HOME/.local/share/mineval-worker/code" python3 -m mineval.remote_api'],
                                    input=json.dumps(request).encode(), stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RemoteError('SSH request failed or timed out; reconcile the job before retrying') from exc
        try:
            response = json.loads(result.stdout)
        except (ValueError, UnicodeError) as exc:
            raise RemoteError('SSH response unavailable; reconcile the job before retrying') from exc
        if result.returncode or not response.get('ok'):
            raise RemoteError('Remote request failed (' + str(response.get('error', 'SSH error')) + ')', response.get('error'))
        return response['result']


    def install(self):
        """Install only this Mineval package under the remote user account."""
        package = Path(__file__).parent
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(package.rglob('*')):
                if path.is_symlink():
                    raise ValueError('Linked package files cannot be installed')
                if path.is_file() and (path.suffix == '.py' or
                                       ('web' in path.relative_to(package).parts and path.suffix in ('.html', '.css', '.js'))):
                    archive.write(path, 'mineval/' + path.relative_to(package).as_posix())
        data = output.getvalue()
        expected = hashlib.sha256(data).hexdigest()
        script = """import fcntl,hashlib,io,os,pathlib,shutil,sys,tempfile,zipfile
root=pathlib.Path.home()/'.local/share/mineval-worker'
root.mkdir(parents=True,exist_ok=True,mode=0o700)
root.chmod(0o700)
with (root/'coordinator.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 data=sys.stdin.buffer.read(32*1024*1024+1)
 if len(data)>32*1024*1024: raise ValueError('package too large')
 staging=pathlib.Path(tempfile.mkdtemp(dir=root))
 try:
  with zipfile.ZipFile(io.BytesIO(data)) as z:
   seen=set()
   for member in z.infolist():
    parts=pathlib.PurePosixPath(member.filename).parts
    if not parts or parts[0]!='mineval' or any(p in ('..','.') for p in parts) or member.is_dir() or member.filename in seen: raise ValueError('invalid package')
    seen.add(member.filename)
    target=staging.joinpath(*parts)
    target.parent.mkdir(parents=True,exist_ok=True)
    with z.open(member) as src,target.open('xb') as dst: shutil.copyfileobj(src,dst)
  backup=root/'code.previous'
  if backup.exists(): shutil.rmtree(backup)
  if (root/'code').exists(): os.rename(root/'code',backup)
  os.rename(staging,root/'code')
  if backup.exists(): shutil.rmtree(backup)
 finally:
  if staging.exists(): shutil.rmtree(staging)
 print(hashlib.sha256(data).hexdigest())
"""
        try:
            result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                                     '-o', 'ConnectTimeout=10', '--', self.host,
                                     'python3 -c ' + shlex.quote(script)], input=data,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RemoteError('SSH worker installation failed') from exc
        if result.returncode or result.stdout.decode(errors='replace').strip() != expected:
            raise RemoteError('Worker installation failed or a coordinator is active')
        return {'installed': True, 'sha256': expected}

    def provision_auth(self, path):
        with tempfile.TemporaryDirectory() as temp:
            validated = Path(temp) / 'auth.json'
            provision_auth(path, validated)
            return self.request({'action': 'provision_auth', 'auth': read_json(validated)})
