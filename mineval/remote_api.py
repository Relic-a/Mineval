"""SSH-invoked private worker API. No network listener or password service."""
import base64
import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
import time
import re
import subprocess
from uuid import uuid4

from .agents import provision_auth
from .artifacts import read_json, write_json, lock, digest
from .preparation import identifier
from . import remote_worker, remote_coordinator, transfer


def dispatch(root, request):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    action = request.get('action')
    if action == 'health':
        from .sandbox import DockerSandbox
        runtime = DockerSandbox()
        try:
            runtime.check()
        except Exception:
            return {'ready': False, 'reason': 'Rootless Docker or required resource controllers are unavailable'}
        image = request.get('image')
        if image:
            try:
                resolved = runtime.image(image)
            except Exception:
                return {'ready': False, 'reason': 'The requested image is not available on the rootless daemon'}
            return {'ready': True, 'image_id': resolved['Id']}
        return {'ready': True}
    if action == 'provision_auth':
        # Only the validated model-only file is installed; no host config travels.
        with tempfile.TemporaryDirectory(dir=root) as temp:
            source = Path(temp) / 'source.json'
            write_json(source, request['auth'])
            source.chmod(0o600)
            provision_auth(source, root / 'auth.json')
        return {'configured': True}
    job_id = identifier(request.get('job_id'))
    directory = root / 'jobs' / job_id
    if action == 'status':
        coordinator = remote_coordinator.Coordinator(root)
        job = next((j for j in coordinator.jobs() if j['id'] == job_id), None)
        if job is None:
            return read_json(directory / 'state.json')
        if (directory / 'stage-active').exists() and job['state'] in ('completed', 'failed', 'interrupted'):
            job['state'] = 'running'
        if job['state'] in ('queued', 'running'):
            remote_coordinator.ensure_running(root)
        return {key: job[key] for key in ('id', 'state', 'stage', 'detail', 'payload', 'updated')}
    if action == 'retry':
        coordinator = remote_coordinator.Coordinator(root)
        job = next((j for j in coordinator.jobs() if j['id'] == job_id), None)
        if job is None:
            if request.get('new_attempt') or read_json(directory / 'state.json').get('state') != 'ready':
                raise ValueError('Remote submission cannot be safely resumed')
            remote_coordinator.enqueue(root, read_json(directory / 'input/job.json'))
        else:
            from .workflows import Workflows
            from .preparation import Store
            child = Workflows(Store(directory / 'input/data'))
            child.remote_coordinator = True
            child.path = coordinator.path
            child.retry(job_id, new_attempt=bool(request.get('new_attempt')))
        (directory / 'cancel').unlink(missing_ok=True)
        remote_coordinator.start(root)
        return next(j for j in coordinator.jobs() if j['id'] == job_id)
    if action == 'cancel':
        coordinator = remote_coordinator.Coordinator(root)
        with coordinator.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row or row['state'] not in ('queued', 'running'):
                raise ValueError('Job is not active')
            if row['state'] == 'queued':
                db.execute("UPDATE jobs SET state='interrupted',detail='Cancelled before launch' WHERE id=? AND state='queued'", (job_id,))
            else:
                (directory / 'cancel').touch(mode=0o600, exist_ok=True)
        return {'job_id': job_id, 'state': 'cancelling'}
    if action in ('upload-start', 'upload-chunk', 'upload-finish'):
        with lock(root / 'locks' / (job_id + '.upload'), wait=True):
            return upload(root, job_id, request)
    if action == 'fetch-delete':
        token = identifier(request.get('token'))
        (directory / 'exports' / (token + '.tar.gz')).unlink(missing_ok=True)
        return {'deleted': True}
    if action == 'submit':
        with tempfile.TemporaryDirectory(dir=root) as temp:
            archive, receipt = Path(temp) / 'input.tar.gz', Path(temp) / 'receipt.json'
            archive.write_bytes(base64.b64decode(request['archive'], validate=True))
            write_json(receipt, request['receipt'])
            remote_worker.initialize(root / 'jobs', job_id, archive, receipt)
        job = read_json(directory / 'input/job.json')
        remote_coordinator.enqueue(root, job)
        remote_coordinator.start(root)
        return {'job_id': job_id, 'state': 'queued'}
    if action == 'fetch-chunk':
        token = identifier(request.get('token'))
        offset, size = request.get('offset'), request.get('size')
        if type(offset) is not int or offset < 0 or type(size) is not int or not 1 <= size <= 1024 * 1024:
            raise ValueError('Invalid export range')
        archive = directory / 'exports' / (token + '.tar.gz')
        total = archive.stat().st_size
        if offset > total:
            raise ValueError('Invalid export offset')
        with archive.open('rb') as stream:
            stream.seek(offset)
            chunk = stream.read(size)
        return {'offset': offset, 'total': total, 'data': base64.b64encode(chunk).decode()}
    if action in ('fetch', 'fetch-start'):
        state = dispatch(root, {'action': 'status', 'job_id': job_id})
        if state['state'] not in ('completed', 'failed', 'interrupted'):
            raise ValueError('Job has not reached a terminal state')
        with tempfile.TemporaryDirectory(dir=root) as temp:
            archive, receipt = Path(temp) / 'output.tar.gz', Path(temp) / 'receipt.json'
            job = read_json(directory / 'input/job.json')
            job['payload'] = state.get('payload', job['payload'])
            task = identifier(job['task'])
            data = directory / 'input/data'
            selected = Path(temp) / 'selected'
            selected.mkdir(mode=0o700)
            for namespace in ('tasks', 'drafts'):
                source = data / namespace / task
                if source.exists():
                    (selected / namespace).mkdir()
                    shutil.copytree(source, selected / namespace / task, symlinks=True)
            usage = data / 'resource-usage'
            if usage.exists():
                for path in usage.glob(job_id + '-*.json'):
                    if read_json(path).get('job_id') != job_id:
                        raise ValueError('Resource telemetry identity mismatch')
                    (selected / 'resource-usage').mkdir(exist_ok=True)
                    shutil.copy2(path, selected / 'resource-usage' / path.name, follow_symlinks=False)
            if job['kind'] == 'benchmark' and (data / 'model-reviews.sqlite3').exists():
                from .memory import Memory
                from .preparation import Store
                model = job['payload']['configuration']['model']
                key = 'qualitative:' + task + ':' + job['payload']['evaluation_id']
                observations = [item for item in Memory(Store(data)).history(model, archive=True)
                                if item['event_key'] == key]
                write_json(selected / 'observations.json', observations)
            transfer.create(selected, archive, receipt)
            if action == 'fetch-start':
                token = uuid4().hex
                exports = directory / 'exports'
                exports.mkdir(exist_ok=True, mode=0o700)
                cleanup_exports(root)
                saved = exports / (token + '.tar.gz')
                os.replace(archive, saved)
                saved.chmod(0o400)
                start_export_reaper(root)
                return {'state': state, 'token': token, 'size': saved.stat().st_size,
                        'receipt': read_json(receipt)}
            return {'state': state, 'archive': base64.b64encode(archive.read_bytes()).decode(),
                    'receipt': read_json(receipt)}
    raise ValueError('Unknown remote action')


EXPORT_TTL = 24 * 60 * 60


def cleanup_exports(root):
    for old in (Path(root) / 'jobs').glob('*/exports/*.tar.gz'):
        try:
            if old.stat().st_mtime < time.time() - EXPORT_TTL:
                old.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


def start_export_reaper(root):
    subprocess.Popen([sys.executable, '-m', 'mineval.remote_api', '--reap-exports', str(root)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, start_new_session=True)


def reap_exports(root):
    try:
        with lock(Path(root) / 'export-reaper.lock'):
            while True:
                cleanup_exports(root)
                paths = list((Path(root) / 'jobs').glob('*/exports/*.tar.gz'))
                expirations = []
                for path in paths:
                    try:
                        expirations.append(path.stat().st_mtime + EXPORT_TTL)
                    except FileNotFoundError:
                        pass
                if not expirations:
                    return
                time.sleep(max(1, min(3600, min(expirations) - time.time() + 1)))
    except ValueError as exc:
        if str(exc) != 'Task is busy in another preparation operation':
            raise


def upload(root, job_id, request):
    directory = root / 'jobs' / job_id
    action = request['action']
    identity_path = directory / 'bundle.json'
    if action == 'upload-start':
        receipt, size = request.get('receipt'), request.get('size')
        if (not isinstance(receipt, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(receipt.get('archive_sha256', '')))
                or type(size) is not int or not 0 < size <= 512 * 1024 * 1024):
            raise ValueError('Invalid upload identity')
        identity = {'receipt': receipt, 'size': size}
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if identity_path.exists():
            if read_json(identity_path) != identity:
                raise ValueError('Remote bundle identity conflict')
        else:
            if any(directory.iterdir()):
                raise ValueError('Existing submission has no bundle identity')
            write_json(identity_path, identity)
        if not (directory / 'state.json').exists():
            write_json(directory / 'state.json', {'state': 'receiving', 'job_id': job_id})
    if not identity_path.exists():
        raise ValueError('Upload has not been started')
    identity = read_json(identity_path)
    partial = directory / 'upload.partial'
    if action == 'upload-chunk':
        offset = request.get('offset')
        data = base64.b64decode(request.get('data', ''), validate=True)
        current = partial.stat().st_size if partial.exists() else 0
        if type(offset) is not int or offset != current or not data or len(data) > 512 * 1024 or current + len(data) > identity['size']:
            raise ValueError('Invalid upload range')
        with partial.open('ab') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    if action == 'upload-finish':
        if not (directory / 'input').exists():
            if not partial.exists() or partial.stat().st_size != identity['size']:
                raise ValueError('Incomplete upload')
            if digest(partial) != identity['receipt']['archive_sha256']:
                partial.unlink()
                return {'job_id': job_id, 'state': 'receiving', 'offset': 0}
            for orphan in directory.glob('.upload-stage-*'):
                if orphan.is_dir() and not orphan.is_symlink():
                    shutil.rmtree(orphan)
            # Verification happens in a private staging directory; a crash leaves
            # the immutable identity and partial available for a safe retry.
            with tempfile.TemporaryDirectory(dir=directory, prefix='.upload-stage-') as temp:
                receipt_path = Path(temp) / 'receipt.json'
                write_json(receipt_path, identity['receipt'])
                remote_worker.initialize_input(job_id, partial, receipt_path, Path(temp) / 'input')
                os.rename(Path(temp) / 'input', directory / 'input')
        write_json(directory / 'state.json', {'state': 'ready', 'job_id': job_id})
        job = read_json(directory / 'input/job.json')
        coordinator = remote_coordinator.Coordinator(root)
        if not any(j['id'] == job_id for j in coordinator.jobs()):
            remote_coordinator.enqueue(root, job)
        partial.unlink(missing_ok=True)
        remote_coordinator.ensure_running(root)
        return {'job_id': job_id, 'state': 'queued'}
    return {'offset': partial.stat().st_size if partial.exists() else 0,
            'complete': (directory / 'input').exists()}


def main():
    # Never inherit a rootful default Docker context on the execution host.
    os.environ.pop('DOCKER_CONTEXT', None)
    os.environ['DOCKER_HOST'] = 'unix:///run/user/' + str(os.getuid()) + '/docker.sock'
    try:
        raw = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('Request exceeds transfer limit')
        request = json.loads(raw)
        result = dispatch(Path.home() / '.local/share/mineval-worker', request)
        json.dump({'ok': True, 'result': result}, sys.stdout)
    except Exception as exc:
        # Never print input or credentials, even for malformed requests.
        json.dump({'ok': False, 'error': type(exc).__name__}, sys.stdout)
        return 1
    return 0


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--reap-exports':
        reap_exports(Path(sys.argv[2]))
    else:
        sys.exit(main())
