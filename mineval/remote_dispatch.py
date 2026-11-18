"""Local durable outbox and remote job reconciliation."""
import base64
import json
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import os
from pathlib import Path
import tempfile
import shutil
from threading import Lock
from uuid import uuid4

from .artifacts import digest, inventory, lock, read_json, write_json
from .remote import RemoteError, SSHClient
from .remote_protocol import grade_snapshot
from .preparation import identifier
from . import transfer

# Archive transfers may take substantially longer than status calls on a slow or
# lossy SSH link. A timeout must still be finite so reconciliation can retry.
TRANSFER_TIMEOUT = 3600
CHUNK_SIZE = 512 * 1024


def export_identity(state):
    payload = state.get('payload', {})
    return (state.get('state'), state.get('updated'), payload.get('run_id'), payload.get('evaluation_id'))


def upload_resumable(client, jid, directory):
    archive = directory / 'input.tar.gz'
    info = client.request({'action': 'upload-start', 'job_id': jid, 'size': archive.stat().st_size,
                           'receipt': read_json(directory / 'receipt.json')}, timeout=TRANSFER_TIMEOUT)
    offset = info['offset']
    if not info.get('complete'):
        with archive.open('rb') as stream:
            stream.seek(offset)
            while offset < archive.stat().st_size:
                data = stream.read(CHUNK_SIZE)
                result = client.request({'action': 'upload-chunk', 'job_id': jid, 'offset': offset,
                                         'data': base64.b64encode(data).decode()}, timeout=TRANSFER_TIMEOUT)
                offset += len(data)
                if result['offset'] != offset:
                    raise ValueError('Invalid upload offset')
    result = client.request({'action': 'upload-finish', 'job_id': jid}, timeout=TRANSFER_TIMEOUT)
    if result.get('state') == 'receiving':
        raise RemoteError('Upload checksum changed; restarting transfer')


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _finish_export_switch(directory):
    marker = directory / 'export-switch.json'
    if not marker.exists():
        return
    saved = read_json(marker)
    backup = directory / 'export-backups' / identifier(saved['backup'])
    if (not (backup / 'download.json').is_file() or not (backup / 'download.partial').is_file()
            or digest(backup / 'download.json') != saved['descriptor_sha256']
            or digest(backup / 'download.partial') != saved['partial_sha256']):
        raise ValueError('Saved export backup is missing or corrupt')
    (directory / 'download.json').unlink(missing_ok=True)
    (directory / 'download.partial').unlink(missing_ok=True)
    _sync_directory(directory)
    marker.unlink()
    _sync_directory(directory)


def _refresh_export(directory, state):
    descriptor, partial = directory / 'download.json', directory / 'download.partial'
    if not descriptor.exists():
        return
    info = read_json(descriptor)
    if 'delta' in info or info.get('state', {}).get('state') == 'graded':
        return
    if (state is None or state.get('state') not in ('completed', 'failed', 'interrupted') or
            export_identity(info.get('state', {})) != export_identity(state)):
        raise ValueError('Legacy export does not match the terminal remote job')
    if not partial.is_file() or partial.is_symlink() or descriptor.is_symlink():
        raise ValueError('Legacy export partial is unavailable')
    backups = directory / 'export-backups'
    backups.mkdir(exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=backups) as temp:
        staged = Path(temp)
        for source in (descriptor, partial):
            target = staged / source.name
            shutil.copyfile(source, target, follow_symlinks=False)
            if digest(source) != digest(target):
                raise ValueError('Legacy export changed during backup')
            target.chmod(0o400)
            with target.open('rb') as stream:
                os.fsync(stream.fileno())
        _sync_directory(staged)
        name = uuid4().hex
        os.rename(staged, backups / name)
        _sync_directory(backups)
    backup = backups / name
    marker = directory / 'export-switch.json'
    write_json(marker, {'backup': name, 'descriptor_sha256': digest(backup / 'download.json'),
                        'partial_sha256': digest(backup / 'download.partial')})
    with marker.open('rb') as stream:
        os.fsync(stream.fileno())
    _sync_directory(directory)
    _finish_export_switch(directory)


def fetch_resumable(client, jid, directory, state=None, *, known_inventory=None, refresh_export=False):
    """Keep a verified export identity and durable byte offset across disconnects."""
    descriptor = directory / 'download.json'
    partial = directory / 'download.partial'
    pending_switch = (directory / 'export-switch.json').exists()
    _finish_export_switch(directory)
    if refresh_export and not pending_switch:
        _refresh_export(directory, state)
    if descriptor.exists() and state is not None:
        saved = read_json(descriptor)
        if export_identity(saved.get('state', {})) != export_identity(state):
            descriptor.unlink()
            partial.unlink(missing_ok=True)
    if descriptor.exists():
        info = read_json(descriptor)
    else:
        action = 'fetch-grade-start' if state is not None and state.get('state') == 'graded' else 'fetch-start'
        request = {'action': action, 'job_id': jid}
        if known_inventory is not None:
            known = known_inventory()
            # The worker caps requests at 16 MiB. Fall back to a full export.
            if len(json.dumps(known).encode()) < 8 * 1024 * 1024:
                request['known_inventory'] = known
        info = client.request(request, timeout=TRANSFER_TIMEOUT)
        if state is not None and export_identity(info.get('state', {})) != export_identity(state):
            raise RemoteError('Remote attempt changed while preparing export')
        if (not isinstance(info.get('size'), int) or info['size'] < 0 or
                not isinstance(info.get('token'), str)):
            raise ValueError('Invalid remote export descriptor')
        write_json(descriptor, info)
    if not partial.exists():
        partial.touch(mode=0o600)
    offset = partial.stat().st_size
    if offset > info['size']:
        raise ValueError('Remote export partial exceeds declared size')
    while offset < info['size']:
        try:
            result = client.request({'action': 'fetch-chunk', 'job_id': jid, 'token': info['token'],
                                     'offset': offset, 'size': min(CHUNK_SIZE, info['size'] - offset)},
                                    timeout=TRANSFER_TIMEOUT)
        except RemoteError as exc:
            if exc.code == 'FileNotFoundError':
                descriptor.unlink(missing_ok=True)
                partial.unlink(missing_ok=True)
            raise

        data = base64.b64decode(result['data'], validate=True)
        if (result.get('offset') != offset or result.get('total') != info['size'] or
                not data or len(data) > min(CHUNK_SIZE, info['size'] - offset)):
            raise ValueError('Invalid remote export chunk')
        with partial.open('ab') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        offset += len(data)
    return info, partial


def outbox(workflow, job):
    root = workflow.store.root / 'remote-outbox'
    root.mkdir(exist_ok=True, mode=0o700)
    final = root / job['id']
    if final.exists():
        receipt = read_json(final / 'receipt.json')
        saved_job = read_json(final / 'input/job.json')
        if (digest(final / 'input.tar.gz') != receipt.get('archive_sha256') or
                any(saved_job.get(k) != job.get(k) for k in ('id', 'kind', 'task')) or
                any(saved_job['payload'].get(k) != job['payload'].get(k) for k in ('revision', 'selection', 'configuration'))):
            raise ValueError('Remote outbox identity or checksum mismatch')
        return final
    with tempfile.TemporaryDirectory(dir=root) as temp:
        temp = Path(temp)
        source = temp / 'input'
        wire_job = {k: job[k] for k in ('id', 'kind', 'task', 'payload')}
        if job['kind'] == 'prepare':
            selected = workflow.settings.get('evaluation_configs', {}).get(job['task'], workflow.settings.get('evaluation_config'))
            config = workflow.eval_config(job['task']) if selected else None
            transfer.preparation_input(workflow.store, wire_job, source, workflow.settings, config)
        else:
            transfer.benchmark_input(workflow.store, wire_job, source, workflow.settings)
        transfer.create(source, temp / 'input.tar.gz', temp / 'receipt.json')
        (temp / 'input.tar.gz').chmod(0o444)
        (temp / 'receipt.json').chmod(0o444)
        os.rename(temp, final)
    return final


def run_job(workflow, job, stop, refresh_export=False, *, schedule_review=None):
    client = SSHClient(workflow.settings['vps']['host'])
    client.stop_event = stop
    jid = job['id']
    transport_only = refresh_export
    try:
        if refresh_export:
            directory = workflow.store.root / 'remote-outbox' / jid
            if not directory.is_dir():
                raise ValueError('No saved remote outbox for export refresh')
        else:
            directory = outbox(workflow, job)
    except Exception as exc:
        workflow.update(jid, state='failed', detail='Could not stage remote inputs (' + type(exc).__name__ + ')')
        return
    if not refresh_export:
        workflow.update(jid, state='running', stage='connecting to VPS', detail='')
    while not stop.is_set():
        try:
            try:
                state = client.request({'action': 'status', 'job_id': jid})
            except RemoteError as exc:
                if transport_only or exc.code != 'FileNotFoundError':
                    raise
                upload_resumable(client, jid, directory)
                continue
            if transport_only and state['state'] not in ('completed', 'failed', 'interrupted'):
                return
            if state['state'] in ('receiving', 'ready'):
                upload_resumable(client, jid, directory)
                continue
            active = state['state'] in ('queued', 'running')
            grade = grade_snapshot(state) if active and state.get('grade_ready') else None
            grade_checkpoint = directory / 'synced-grade.json'
            grade_synced = grade is not None and grade_checkpoint.exists() and read_json(grade_checkpoint) == grade
            if active and (grade is None or grade_synced):
                if (grade_synced and schedule_review is not None and
                        not read_json(directory / 'input/settings.json').get('architecture_review')):
                    schedule_review(job | {'payload': grade['payload']})
                workflow.update(jid, stage='VPS: ' + state.get('stage', state['state']), detail=state.get('detail', ''))
                stop.wait(5)
                continue
            if grade is None and state['state'] not in ('completed', 'failed', 'interrupted'):
                workflow.update(jid, state='interrupted', detail='Remote job state is uncertain; inspect the VPS before retrying.')
                return
            stage = 'downloading VPS grade' if grade else 'downloading VPS evidence'
            workflow.update(jid, stage=stage, detail='Resuming verified export.')
            refreshing = refresh_export
            refresh_export = False
            result, downloaded = fetch_resumable(client, jid, directory, grade or state,
                known_inventory=lambda: transfer.local_inventory(workflow.store, job['task'], jid),
                refresh_export=refreshing)
            latest = client.request({'action': 'status', 'job_id': jid})
            if grade is not None:
                latest = grade_snapshot(latest) or {}
            if export_identity(latest) != export_identity(result['state']):
                (directory / 'download.json').unlink(missing_ok=True)
                downloaded.unlink(missing_ok=True)
                continue
            state = result['state']
            result_job = job | {'payload': state.get('payload', job['payload'])}
            imported_grade = False
            with tempfile.TemporaryDirectory(dir=workflow.store.root) as temp:
                temp = Path(temp)
                archive, receipt = temp / 'output.tar.gz', temp / 'receipt.json'
                import shutil
                shutil.copyfile(downloaded, archive)
                write_json(receipt, result['receipt'])
                verified = transfer.verify(archive, receipt, temp / 'data')
                if 'delta' in result:
                    transfer.reconstruct_delta(temp / 'data', result['delta'], verified, workflow.store, job['task'], jid)
                receipts = directory / 'receipts'
                receipts.mkdir(exist_ok=True, mode=0o700)
                saved_receipt = receipts / (verified['archive_sha256'] + '.json')
                if saved_receipt.exists():
                    if read_json(saved_receipt) != verified:
                        raise ValueError('Remote transfer receipt conflicts with saved evidence')
                else:
                    write_json(saved_receipt, verified)
                    saved_receipt.chmod(0o444)
                provenance = directory / 'export-descriptors'
                provenance.mkdir(exist_ok=True, mode=0o700)
                descriptor_path = provenance / (identifier(result['token']) + '.json')
                if descriptor_path.exists():
                    if read_json(descriptor_path) != result:
                        raise ValueError('Remote export descriptor conflicts with saved evidence')
                else:
                    write_json(descriptor_path, result)
                    descriptor_path.chmod(0o444)
                if job['kind'] == 'prepare':
                    checkpoint = directory / 'synced-baseline.json'
                    baseline = read_json(checkpoint if checkpoint.exists() else directory / 'input/baseline.json')
                    transfer.import_preparation(workflow.store, temp / 'data', job, baseline)
                    write_json(checkpoint, {'task': inventory(temp / 'data/tasks' / job['task']),
                                            'draft': inventory(temp / 'data/drafts' / job['task'])})
                elif job['kind'] == 'benchmark':
                    run = temp / 'data/tasks' / job['task'] / 'runs' / (result_job['payload']['run_id'] + '.json')
                    if run.exists():
                        transfer.import_benchmark(workflow.store, temp / 'data', result_job)
                        evaluation_id = state.get('payload', job['payload'])['evaluation_id']
                        evaluation_path = workflow.store.task(job['task']) / 'evaluations' / (evaluation_id + '.json')
                        if evaluation_path.exists():
                            from .memory import Memory, evaluation_source, record_evaluation
                            record_evaluation(workflow.store, job['task'], evaluation_id)
                            imported_grade = True
                            observations = temp / 'data/observations.json'
                            if observations.exists():
                                model, source, _ = evaluation_source(workflow.store, job['task'], evaluation_id)
                                expected_key = 'qualitative:' + job['task'] + ':' + evaluation_id
                                for observation in read_json(observations):
                                    if (observation['event_key'] != expected_key or observation['model'] != model
                                            or observation['sources'] != [source]):
                                        raise ValueError('Remote review source mismatch')
                                    Memory(workflow.store).append(model, observation['body'], [source], expected_key)
                usage = temp / 'data/resource-usage'
                if usage.exists():
                    target = workflow.store.root / 'resource-usage'
                    target.mkdir(exist_ok=True)
                    for source in usage.glob(jid + '-*.json'):
                        if read_json(source).get('job_id') != jid:
                            raise ValueError('Resource telemetry identity mismatch')
                        destination = target / source.name
                        if destination.exists():
                            if digest(source) != digest(destination):
                                raise ValueError('Resource telemetry conflict')
                        else:
                            with tempfile.NamedTemporaryFile(dir=target, delete=False) as staged:
                                name = staged.name
                                with source.open('rb') as stream:
                                    import shutil
                                    shutil.copyfileobj(stream, staged)
                            try:
                                if digest(name) != digest(source):
                                    raise ValueError('Resource telemetry changed during import')
                                os.replace(name, destination)
                            finally:
                                if os.path.exists(name):
                                    os.unlink(name)
            if grade is not None:
                if not imported_grade:
                    raise ValueError('Remote grade export is missing its evaluation')
                workflow.update(jid, payload=grade['payload'])
                write_json(grade_checkpoint, grade)
            if (imported_grade and schedule_review is not None and
                    (grade is None or not read_json(directory / 'input/settings.json').get('architecture_review'))):
                schedule_review(result_job)
            try:
                client.request({'action': 'fetch-delete', 'job_id': jid, 'token': result['token']})
            except RemoteError:
                pass  # Expiration removes abandoned exports.
            downloaded.unlink(missing_ok=True)
            (directory / 'download.json').unlink(missing_ok=True)
            if grade is not None:
                continue
            workflow.update(jid, state=state['state'], stage=state.get('stage', state['state']),
                            detail=state.get('detail', ''), payload=state.get('payload', job['payload']))
            return
        except RemoteError as exc:
            if exc.code and exc.code != 'FileNotFoundError':
                workflow.update(jid, state='interrupted', stage='remote submission or import needs attention',
                                detail='Remote operation rejected (' + exc.code + '); evidence remains on the VPS.')
                return
            workflow.update(jid, stage='VPS connection unavailable', detail='Reconnecting without restarting the remote job.')
            stop.wait(3)
        except Exception as exc:
            if isinstance(exc, ValueError) and str(exc) == 'Task is busy in another preparation operation':
                stop.wait(1)
                continue
            from .devlog import failure
            failure('remote.import.failed', exc, job_id=jid)
            workflow.update(jid, state='interrupted', stage='remote import needs attention',
                            detail='Remote result could not be imported (' + type(exc).__name__ + '); evidence remains on the VPS.')
            return


def _review_architecture(workflow, stop, path, request):
    from . import architecture
    from .devlog import failure
    from .sandbox import DockerSandbox

    task, payload = request['task'], request['payload']
    if stop.is_set():
        return
    DockerSandbox._cancellation.event = stop
    try:
        run = workflow.store.inspect_run(task, payload['run_id'])
        settings = workflow.settings
        image = settings.get('review_image') or settings.get('preparation_image') or run['operation'].get('configured_image')
        if not image:
            raise ValueError('Architecture review requires a configured review image')
        role = workflow.role_adapter('evaluation', payload)
        options = dict(image=image, context=workflow.context, auth_file=workflow.auth_file,
                       model=settings.get('review_model', role.model), effort=settings.get('review_effort', role.effort))
        architecture.record(workflow.store, task, payload['run_id'], **options)
        architecture.compare(workflow.store, task, run['prepared_revision']['version'], **options)
    except KeyboardInterrupt:
        pass  # Leave the request available for the next worker invocation.
    except Exception as exc:
        failure('remote.architecture.failed', exc, job_id=request['id'], run_id=payload['run_id'])
        write_json(path, request | {'state': 'failed'})
    else:
        write_json(path, request | {'state': 'completed'})
    finally:
        DockerSandbox._cancellation.event = None


def dispatch(workflow, stop, job_id=None, refresh_export=False):
    try:
        with lock(workflow.store.root / 'remote-dispatcher.lock'):
            with (ThreadPoolExecutor(max_workers=16, thread_name_prefix='mineval-vps') as pool,
                  ThreadPoolExecutor(max_workers=1, thread_name_prefix='mineval-architecture') as reviews):
                review_root = workflow.store.root / 'remote-outbox'
                review_lock = Lock()
                scheduled = set()

                def schedule_review(job):
                    from .devlog import failure

                    payload = job['payload']
                    if payload['configuration']['adapter'] == 'fake':
                        return
                    path = review_root / identifier(job['id']) / ('architecture-' + identifier(payload['run_id']) + '.json')
                    try:
                        with review_lock:
                            if path in scheduled or (path.exists() and read_json(path)['state'] != 'pending'):
                                return
                            request = {key: job[key] for key in ('id', 'task', 'payload')} | {'state': 'pending'}
                            write_json(path, request)
                            reviews.submit(_review_architecture, workflow, stop, path, request)
                            scheduled.add(path)
                    except (OSError, ValueError) as exc:
                        failure('remote.architecture.schedule_failed', exc, job_id=job['id'])

                review_options = {}
                if workflow.settings.get('architecture_review', False) and not refresh_export:
                    review_options['schedule_review'] = schedule_review
                    for path in review_root.glob((job_id or '*') + '/architecture-*.json'):
                        try:
                            request = read_json(path)
                            if request['state'] == 'pending':
                                schedule_review(request)
                        except (OSError, ValueError, KeyError, TypeError) as exc:
                            from .devlog import failure
                            failure('remote.architecture.recovery_failed', exc)
                active = {}
                while not stop.is_set():
                    for job in reversed(workflow.jobs()):
                        if ((job_id is None or job['id'] == job_id) and job['state'] in (('queued', 'running', 'interrupted') if refresh_export else ('queued', 'running')) and job['payload'].get('execution_host') == 'vps'
                                and job['id'] not in active):
                            args = (workflow, job, stop, True) if refresh_export else (workflow, job, stop)
                            active[job['id']] = pool.submit(run_job, *args, **review_options)
                    if not active:
                        return
                    done, _ = wait(active.values(), timeout=0.5, return_when=FIRST_COMPLETED)
                    for jid, future in list(active.items()):
                        if future in done:
                            del active[jid]
                            future.result()
                    if refresh_export and not active:
                        return
    except ValueError as exc:
        if str(exc) != 'Task is busy in another preparation operation':
            raise
