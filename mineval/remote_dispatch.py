"""Local durable outbox and remote job reconciliation."""
import base64
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import os
from pathlib import Path
import tempfile

from .artifacts import digest, inventory, lock, read_json, write_json
from .remote import RemoteError, SSHClient
from . import transfer


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


def run_job(workflow, job, stop):
    client = SSHClient(workflow.settings['vps']['host'])
    jid = job['id']
    try:
        directory = outbox(workflow, job)
    except Exception as exc:
        workflow.update(jid, state='failed', detail='Could not stage remote inputs (' + type(exc).__name__ + ')')
        return
    workflow.update(jid, state='running', stage='connecting to VPS', detail='')
    while not stop.is_set():
        try:
            try:
                state = client.request({'action': 'status', 'job_id': jid})
            except RemoteError as exc:
                if exc.code != 'FileNotFoundError':
                    raise
                client.request({'action': 'submit', 'job_id': jid,
                                'archive': base64.b64encode((directory / 'input.tar.gz').read_bytes()).decode(),
                                'receipt': read_json(directory / 'receipt.json')}, timeout=300)
                continue
            if state['state'] == 'ready':
                workflow.update(jid, state='interrupted', stage='remote submission uncertain',
                                detail='Remote input exists without a queued job; inspect the VPS before retrying.')
                return
            if state['state'] in ('queued', 'running'):
                workflow.update(jid, stage='VPS: ' + state.get('stage', state['state']), detail=state.get('detail', ''))
                stop.wait(1)
                continue
            if state['state'] not in ('completed', 'failed', 'interrupted'):
                workflow.update(jid, state='interrupted', detail='Remote job state is uncertain; inspect the VPS before retrying.')
                return
            result = client.request({'action': 'fetch', 'job_id': jid}, timeout=300)
            with tempfile.TemporaryDirectory(dir=workflow.store.root) as temp:
                temp = Path(temp)
                archive, receipt = temp / 'output.tar.gz', temp / 'receipt.json'
                archive.write_bytes(base64.b64decode(result['archive'], validate=True))
                write_json(receipt, result['receipt'])
                verified = transfer.verify(archive, receipt, temp / 'data')
                receipts = directory / 'receipts'
                receipts.mkdir(exist_ok=True, mode=0o700)
                saved_receipt = receipts / (verified['archive_sha256'] + '.json')
                if saved_receipt.exists():
                    if read_json(saved_receipt) != verified:
                        raise ValueError('Remote transfer receipt conflicts with saved evidence')
                else:
                    write_json(saved_receipt, verified)
                    saved_receipt.chmod(0o444)
                if job['kind'] == 'prepare':
                    checkpoint = directory / 'synced-baseline.json'
                    baseline = read_json(checkpoint if checkpoint.exists() else directory / 'input/baseline.json')
                    transfer.import_preparation(workflow.store, temp / 'data', job, baseline)
                    write_json(checkpoint, {'task': inventory(temp / 'data/tasks' / job['task']),
                                            'draft': inventory(temp / 'data/drafts' / job['task'])})
                elif job['kind'] == 'benchmark':
                    run = temp / 'data/tasks' / job['task'] / 'runs' / (job['payload']['run_id'] + '.json')
                    if run.exists():
                        transfer.import_benchmark(workflow.store, temp / 'data', job)
                        evaluation_id = state.get('payload', job['payload'])['evaluation_id']
                        evaluation_path = workflow.store.task(job['task']) / 'evaluations' / (evaluation_id + '.json')
                        if evaluation_path.exists():
                            from .memory import Memory, evaluation_source, record_evaluation
                            record_evaluation(workflow.store, job['task'], evaluation_id)
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
            workflow.update(jid, state=state['state'], stage=state.get('stage', state['state']),
                            detail=state.get('detail', ''), payload=state.get('payload', job['payload']))
            return
        except RemoteError:
            workflow.update(jid, stage='VPS connection unavailable', detail='Reconnecting without restarting the remote job.')
            stop.wait(3)
        except Exception as exc:
            workflow.update(jid, state='interrupted', stage='remote import needs attention',
                            detail='Remote result could not be imported (' + type(exc).__name__ + '); evidence remains on the VPS.')
            return


def dispatch(workflow, stop):
    try:
        with lock(workflow.store.root / 'remote-dispatcher.lock'):
            with ThreadPoolExecutor(max_workers=16, thread_name_prefix='mineval-vps') as pool:
                active = {}
                while not stop.is_set():
                    for job in reversed(workflow.jobs()):
                        if (job['state'] in ('queued', 'running') and job['payload'].get('execution_host') == 'vps'
                                and job['id'] not in active):
                            active[job['id']] = pool.submit(run_job, workflow, job, stop)
                    if not active:
                        return
                    done, _ = wait(active.values(), timeout=0.5, return_when=FIRST_COMPLETED)
                    for jid, future in list(active.items()):
                        if future in done:
                            del active[jid]
                            future.result()
    except ValueError as exc:
        if str(exc) != 'Task is busy in another preparation operation':
            raise
