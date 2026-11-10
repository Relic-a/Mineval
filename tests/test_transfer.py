import json
from pathlib import Path
import tempfile
import unittest

from mineval import transfer


class SSHCancellationTests(unittest.TestCase):
    def test_slow_response_keeps_request_and_response_intact(self):
        import os
        from unittest.mock import patch
        from mineval.remote import SSHClient
        with tempfile.TemporaryDirectory() as temp:
            ssh = Path(temp) / 'ssh'
            ssh.write_text('#!/usr/bin/env python3\nimport json,sys,time\n'
                           'request=json.load(sys.stdin)\ntime.sleep(1.2)\n'
                           'json.dump({"ok": True, "result": request}, sys.stdout)\n')
            ssh.chmod(0o755)
            with patch.dict(os.environ, {'PATH': temp + os.pathsep + os.environ['PATH']}):
                self.assertEqual(SSHClient('test@example.test').request({'action': 'status'}, timeout=5),
                                 {'action': 'status'})

    def test_cancellation_stops_local_ssh_without_waiting_for_transfer_timeout(self):
        import os
        import threading
        import time
        from unittest.mock import patch
        from mineval.remote import SSHClient, RemoteError
        with tempfile.TemporaryDirectory() as temp:
            ssh = Path(temp) / 'ssh'
            ssh.write_text('#!/bin/sh\nexec sleep 30\n')
            ssh.chmod(0o755)
            client = SSHClient('test@example.test')
            client.stop_event = threading.Event()
            timer = threading.Timer(0.2, client.stop_event.set)
            start = time.monotonic()
            with patch.dict(os.environ, {'PATH': temp + os.pathsep + os.environ['PATH']}):
                timer.start()
                try:
                    with self.assertRaisesRegex(RemoteError, 'cancelled'):
                        client.request({'action': 'status'}, timeout=60)
                finally:
                    timer.cancel()
            self.assertLess(time.monotonic() - start, 4)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.archive, self.receipt = self.root / 'bundle.tar.gz', self.root / 'receipt.json'

    def test_roundtrip_and_corruption(self):
        (self.source / 'private').mkdir()
        (self.source / 'private' / 'evidence').write_bytes(b'private evidence')
        transfer.create(self.source, self.archive, self.receipt)
        transfer.verify(self.archive, self.receipt, self.root / 'result')
        self.assertEqual((self.root / 'result/private/evidence').read_bytes(), b'private evidence')
        self.archive.write_bytes(self.archive.read_bytes() + b'corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            transfer.verify(self.archive, self.receipt, self.root / 'rejected')
        self.assertFalse((self.root / 'rejected').exists())

    def test_rejects_receipt_mismatch_and_links(self):
        (self.source / 'file').write_text('value')
        transfer.create(self.source, self.archive, self.receipt)
        data = json.loads(self.receipt.read_text())
        data['files']['file']['sha256'] = '0' * 64
        self.receipt.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'contents'):
            transfer.verify(self.archive, self.receipt, self.root / 'rejected')
        self.assertFalse((self.root / 'rejected').exists())
        (self.source / 'link').symlink_to(self.source / 'file')
        with self.assertRaisesRegex(ValueError, 'Links'):
            transfer.create(self.source, self.archive, self.receipt)


class RemoteWorkerTests(unittest.TestCase):
    def test_job_identity_is_durable_and_only_started_once(self):
        from unittest.mock import patch
        from mineval import remote_worker
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            source.mkdir()
            (source / 'job.json').write_text(json.dumps({'id': 'job1', 'kind': 'prepare', 'task': 'task1', 'payload': {}}))
            (source / 'settings.json').write_text('{}')
            archive, receipt = root / 'input.tar.gz', root / 'receipt.json'
            transfer.create(source, archive, receipt)
            directory = remote_worker.initialize(root / 'jobs', 'job1', archive, receipt)
            with self.assertRaises(FileExistsError):
                remote_worker.initialize(root / 'jobs', 'job1', archive, receipt)
            from mineval import remote_coordinator
            job = json.loads((directory / 'input/job.json').read_text())
            remote_coordinator.enqueue(root / 'worker', job)
            with self.assertRaises(Exception):
                remote_coordinator.enqueue(root / 'worker', job)


    def test_remote_api_provisions_only_model_auth(self):
        from mineval.remote_api import dispatch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'worker'
            result = dispatch(root, {'action': 'provision_auth', 'auth': {'OPENAI_API_KEY': 'test-secret'}})
            self.assertTrue(result['configured'])
            self.assertEqual((root / 'auth.json').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                dispatch(root, {'action': 'provision_auth', 'auth': {'unexpected': 'secret'}})
            self.assertNotIn('test-secret', str(result))


from test_preparation import HistoryFixture
from test_agents import SimulatedDocker
from mineval import benchmark
from mineval.agents import FakeAdapter
from mineval.preparation import Store
import shutil


class BenchmarkImportTests(HistoryFixture):
    def test_remote_attempt_import_is_idempotent_and_rejects_conflict(self):
        version = self.freeze('example', self.review)['version']
        revision = self.store.inspect_revision('example', version)
        remote_root = self.root / 'remote-store'
        shutil.copytree(self.store.root, remote_root)
        remote = Store(remote_root)
        benchmark.run(remote, 'example', version, run_id='remote-run',
                      adapter=FakeAdapter(effort='medium'), sandbox=SimulatedDocker())
        job = {'task': 'example', 'payload': {'run_id': 'remote-run', 'evaluation_id': 'eval1',
               'revision': revision, 'configuration': {'model': 'fake', 'effort': 'medium', 'adapter': 'fake'}}}
        from mineval.remote_dispatch import outbox
        from mineval.workflows import Workflows
        staged_job = job | {'id': 'outbox1', 'kind': 'benchmark'}
        directory = outbox(Workflows(self.store, {'secret': 'PRIVATE-CREDENTIAL-MARKER'}), staged_job)
        self.assertNotIn(b'PRIVATE-CREDENTIAL-MARKER', (directory / 'input.tar.gz').read_bytes())
        self.assertFalse(any('auth.json' in str(path) for path in (directory / 'input').rglob('*')))
        (directory / 'input.tar.gz').chmod(0o600)
        with (directory / 'input.tar.gz').open('ab') as stream:
            stream.write(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            outbox(Workflows(self.store, {}), staged_job)
        snapshot = self.root / 'input'
        transfer.benchmark_input(self.store, job | {'id': 'job1', 'kind': 'benchmark'}, snapshot,
                                 {'auth_file': '/secret', 'qualitative_review': False})
        self.assertFalse((snapshot / 'data/tasks/example/private').exists())
        self.assertNotIn('auth_file', json.loads((snapshot / 'settings.json').read_text()))
        self.assertEqual(Store(snapshot / 'data').inspect_revision('example', version), revision)
        transfer.import_benchmark(self.store, remote_root, job)
        self.assertEqual(self.store.inspect_run('example', 'remote-run')['status'], 'completed')
        transfer.import_benchmark(self.store, remote_root, job)
        local = self.store.task('example') / 'runs/remote-run.json'
        local.chmod(0o600)
        local.write_text('conflict')
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            transfer.import_benchmark(self.store, remote_root, job)


class CoordinatorTests(unittest.TestCase):
    def test_shared_queue_routes_jobs_to_isolated_stores(self):
        from unittest.mock import patch
        from mineval import remote_coordinator
        import threading
        import time
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'settings.json').write_text(json.dumps({'concurrency': {'total': 2, 'prepare': 2},
                                                           'resources': {'admission_interval_seconds': 0.01}}))
            for jid in ('job1', 'job2'):
                path = root / 'jobs' / jid / 'input'
                path.mkdir(parents=True)
                (path / 'settings.json').write_text('{}')
                remote_coordinator.enqueue(root, {'id': jid, 'kind': 'prepare', 'task': jid, 'payload': {}})
            seen = []
            guard = threading.Lock()
            def stage(workflow, job, phase):
                with guard:
                    seen.append((job['id'], workflow.store.root))
                time.sleep(0.05)
                workflow.update(job['id'], state='completed', stage='completed')
            with patch('mineval.workflows.Workflows._work_stage', stage), patch('mineval.resources.admits', return_value=True):
                coordinator = remote_coordinator.Coordinator(root)
                coordinator.work()
            self.assertEqual({j['state'] for j in coordinator.jobs()}, {'completed'})
            self.assertEqual({path for _, path in seen}, {root / 'jobs/job1/input/data', root / 'jobs/job2/input/data'})


class PreparationImportTests(HistoryFixture):
    def test_completed_remote_revision_import_and_conflict(self):
        job = {'id': 'job1', 'kind': 'prepare', 'task': 'example', 'payload': {}}
        snapshot = self.root / 'prep-input'
        transfer.preparation_input(self.store, job, snapshot, {})
        baseline = json.loads((snapshot / 'baseline.json').read_text())
        local = self.store
        self.store = Store(snapshot / 'data')
        try:
            version = self.freeze('example', self.review)['version']
        finally:
            self.store = local
        revision = transfer.import_preparation(local, snapshot / 'data', job, baseline)
        self.assertEqual(local.inspect_revision('example', version), revision)
        self.assertEqual(transfer.import_preparation(local, snapshot / 'data', job, baseline), revision)
        (local.draft('example') / 'workspace' / 'conflict').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'draft changed'):
            transfer.import_preparation(local, snapshot / 'data', job, baseline)


class DispatchTests(HistoryFixture):
    def test_download_resumes_after_disconnection_without_new_export(self):
        import base64
        from mineval.remote import RemoteError
        from mineval.remote_dispatch import CHUNK_SIZE, fetch_resumable
        content = b'a' * CHUNK_SIZE + b'end'
        class Client:
            starts = 0
            fail = True
            def request(self, request, timeout=60):
                if request['action'] == 'fetch-start':
                    self.starts += 1
                    return {'token': 'export1', 'size': len(content), 'receipt': {}, 'state': {}}
                offset = request['offset']
                if offset and self.fail:
                    self.fail = False
                    raise RemoteError('connection lost')
                return {'offset': offset, 'total': len(content),
                        'data': base64.b64encode(content[offset:offset + request['size']]).decode()}
        client = Client()
        with self.assertRaises(RemoteError):
            fetch_resumable(client, 'job1', self.root)
        info, partial = fetch_resumable(client, 'job1', self.root)
        self.assertEqual(client.starts, 1)
        self.assertEqual(info['token'], 'export1')
        self.assertEqual(partial.read_bytes(), content)

    def test_remote_dispatch_executes_and_imports_fake_attempt(self):
        from unittest.mock import patch
        from mineval import remote_api, remote_coordinator, remote_dispatch
        from mineval.workflows import Workflows
        import threading
        version = self.freeze('example', self.review)['version']
        revision = self.store.inspect_revision('example', version)
        payload = {'run_id': 'dispatch-run', 'evaluation_id': 'dispatch-eval',
                   'revision': revision, 'selection': {'task_id': 'example', 'version': version},
                   'configuration': {'model': 'fake', 'effort': 'medium', 'adapter': 'fake'},
                   'model_settings': {'preparation': {'model': 'fake', 'effort': 'medium', 'adapter': 'fake'},
                                      'evaluation': {'model': 'fake', 'effort': 'medium', 'adapter': 'fake'}},
                   'execution_host': 'vps', 'evaluation_config': {}}
        from mineval.memory import Memory
        Memory(self.store).append('fake', ['Prior observation'], [{'task_id': 'example'}], 'prior')
        flow = Workflows(self.store, {'vps': {'host': 'test@example.test'}, 'qualitative_review': False,
                                      'resources': {'admission_interval_seconds': 0.01}})
        with flow.db() as db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)', ('job1', 'benchmark', 'example', json.dumps(payload),
                       'queued', 'queued', '', 1, 1, 'dedup'))
            local_payload = payload | {'run_id': 'local-run', 'evaluation_id': 'local-eval', 'execution_host': 'local'}
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)', ('job2', 'benchmark', 'example', json.dumps(local_payload),
                       'queued', 'queued', '', 2, 2, 'dedup2'))
        remote = self.root / 'remote-worker'
        remote.mkdir()
        (remote / 'settings.json').write_text(json.dumps({'resources': {'admission_interval_seconds': 0.01}}))
        threads = []
        class Client:
            submissions = 0
            def __init__(self, host): pass
            def request(self, request, timeout=60):
                try:
                    result = remote_api.dispatch(remote, request)
                    if request['action'] == 'submit':
                        Client.submissions += 1
                        from mineval.remote import RemoteError
                        raise RemoteError('connection lost after submission')
                    return result
                except FileNotFoundError:
                    from mineval.remote import RemoteError
                    raise RemoteError('missing', 'FileNotFoundError')
        def start(root):
            thread = threading.Thread(target=lambda: remote_coordinator.Coordinator(root).work())
            threads.append(thread)
            thread.start()
        def run_stage(workflow, jid, task, p, *, phase=None):
            if phase == 'benchmark':
                benchmark.run(workflow.store, task, version, run_id=p['run_id'],
                              adapter=FakeAdapter(effort='medium'), sandbox=SimulatedDocker())
            elif phase == 'evaluation':
                from mineval import evaluation
                from test_evaluation import EvaluationRunner
                config = workflow.store.root / 'dispatch-evaluation.json'
                config.write_text(json.dumps({'image': 'local:test', 'network': 'none', 'commands': [
                    {'name': 'answer', 'kind': 'tests', 'argv': ['sh', '-c', 'PYTHONPATH=. python3 tests/test_answer.py']}]}))
                evaluation.evaluate(workflow.store, task, p['run_id'], config,
                                    evaluation_id=p['evaluation_id'], sandbox=EvaluationRunner())
            elif phase == 'review':
                from mineval.memory import Memory, evaluation_source
                self.assertTrue(any(item['body'] == ['Prior observation'] for item in Memory(workflow.store).history('fake')))
                model, source, _ = evaluation_source(workflow.store, task, p['evaluation_id'])
                Memory(workflow.store).append(model, ['Evidence-backed review'], [source],
                                              'qualitative:' + task + ':' + p['evaluation_id'])
        with patch('mineval.remote_dispatch.SSHClient', Client), patch('mineval.remote_coordinator.start', start), \
                patch('mineval.workflows.Workflows.benchmark', run_stage), patch('mineval.resources.admits', return_value=True):
            flow.work()
            for thread in threads:
                thread.join()
        self.assertEqual(Client.submissions, 1)
        self.assertTrue(list((self.store.root / 'remote-outbox/job1/receipts').glob('*.json')))
        self.assertEqual({j['state'] for j in flow.jobs()}, {'completed'})
        self.assertEqual(self.store.inspect_run('example', 'local-run')['status'], 'completed')
        self.assertEqual(self.store.inspect_run('example', 'dispatch-run')['status'], 'completed')
        self.assertEqual(self.store.inspect_evaluation('example', 'dispatch-eval')['run_id'], 'dispatch-run')
        from mineval.memory import Memory
        self.assertTrue(any(item['body'] == ['Evidence-backed review'] for item in Memory(self.store).history('fake'))) 
        self.assertTrue(list((self.store.root / 'resource-usage').glob('job1-*.json')))


class RetryTests(HistoryFixture):
    def test_remote_retry_keeps_saved_attempt_and_replaces_uncertain_evaluation(self):
        from unittest.mock import patch
        from mineval import remote_api, remote_coordinator
        version = self.freeze('example', self.review)['version']
        revision = self.store.inspect_revision('example', version)
        worker = self.root / 'worker'
        input_dir = worker / 'jobs/job1/input'
        input_dir.mkdir(parents=True)
        shutil.copytree(self.store.root, input_dir / 'data')
        remote = Store(input_dir / 'data')
        benchmark.run(remote, 'example', version, run_id='saved', adapter=FakeAdapter(effort='medium'), sandbox=SimulatedDocker())
        payload = {'run_id': 'saved', 'evaluation_id': 'uncertain', 'phase': 'evaluation', 'revision': revision, 'execution_host': 'vps',
                   'configuration': {'model': 'fake', 'effort': 'medium', 'adapter': 'fake'}}
        job = {'id': 'job1', 'kind': 'benchmark', 'task': 'example', 'payload': payload}
        remote_coordinator.enqueue(worker, job)
        coordinator = remote_coordinator.Coordinator(worker)
        coordinator.update('job1', state='interrupted')
        with patch('mineval.remote_coordinator.start'):
            retried = remote_api.dispatch(worker, {'action': 'retry', 'job_id': 'job1', 'new_attempt': False})
        self.assertEqual(retried['payload']['run_id'], 'saved')
        self.assertNotEqual(retried['payload']['evaluation_id'], 'uncertain')
        self.assertEqual(remote.inspect_run('example', 'saved')['status'], 'completed')


class SubmissionRecoveryTests(unittest.TestCase):
    def test_ready_but_unqueued_submission_requires_explicit_retry(self):
        from unittest.mock import patch
        from mineval import remote_api, remote_worker
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            source.mkdir()
            job = {'id': 'job1', 'kind': 'prepare', 'task': 'task1', 'payload': {}}
            (source / 'job.json').write_text(json.dumps(job))
            (source / 'settings.json').write_text('{}')
            archive, receipt = root / 'input.tar.gz', root / 'receipt.json'
            transfer.create(source, archive, receipt)
            worker = root / 'worker'
            remote_worker.initialize(worker / 'jobs', 'job1', archive, receipt)
            self.assertEqual(remote_api.dispatch(worker, {'action': 'status', 'job_id': 'job1'})['state'], 'ready')
            with patch('mineval.remote_coordinator.start'):
                result = remote_api.dispatch(worker, {'action': 'retry', 'job_id': 'job1'})
            self.assertEqual(result['state'], 'queued')
            with self.assertRaises(ValueError):
                remote_api.dispatch(worker, {'action': 'retry', 'job_id': 'job1'})


class PreparationRecoveryTests(HistoryFixture):
    def test_interrupted_publication_resumes_and_preserves_draft_checkpoint(self):
        from unittest.mock import patch
        import os
        job = {'id': 'prep-job', 'kind': 'prepare', 'task': 'example', 'payload': {}}
        old_directory = self.store.draft('example') / 'workspace/replaced'
        old_directory.mkdir()
        (old_directory / 'file').write_text('before')
        snapshot = self.root / 'prep-input'
        transfer.preparation_input(self.store, job, snapshot, {})
        baseline = json.loads((snapshot / 'baseline.json').read_text())
        remote = Store(snapshot / 'data')
        draft_file = remote.draft('example') / 'workspace/app.py'
        draft_file.write_text('def answer(): return 7\n')
        (remote.draft('example') / 'workspace/new-file').write_text('checkpoint')
        shutil.rmtree(remote.draft('example') / 'workspace/replaced')
        (remote.draft('example') / 'workspace/replaced').write_text('after')
        (remote.task('example') / 'checkpoint.json').write_text('{"stage":"cleanup"}')
        original = os.replace
        failed = [False]
        def interrupted(src, dst):
            if str(dst) == str(self.store.draft('example') / 'workspace/app.py') and not failed[0]:
                failed[0] = True
                raise OSError('simulated crash')
            return original(src, dst)
        with patch('mineval.transfer.os.replace', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'simulated crash'):
                transfer.import_preparation(self.store, snapshot / 'data', job, baseline)
        with self.assertRaisesRegex(ValueError, 'import needs recovery'):
            with self.store.operation('example'):
                pass
        transfer.import_preparation(self.store, snapshot / 'data', job, baseline)
        with self.store.operation('example'):
            self.assertEqual((self.store.draft('example') / 'workspace/app.py').read_text(), 'def answer(): return 7\n')
            self.assertTrue((self.store.draft('example') / 'workspace/new-file').exists())
            self.assertEqual((self.store.draft('example') / 'workspace/replaced').read_text(), 'after')
            self.assertTrue((self.store.task('example') / 'checkpoint.json').exists())
        from mineval.artifacts import inventory
        synchronized = {'task': inventory(remote.task('example')), 'draft': inventory(remote.draft('example'))}
        draft_file.write_text('def answer(): return 8\n')
        transfer.import_preparation(self.store, snapshot / 'data', job, synchronized)
        self.assertEqual((self.store.draft('example') / 'workspace/app.py').read_text(), 'def answer(): return 8\n')


class CancellationTests(unittest.TestCase):
    def test_queued_and_running_cancellation_are_scoped_to_job(self):
        from mineval import remote_api, remote_coordinator
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for jid in ('job1', 'job2'):
                (root / 'jobs' / jid).mkdir(parents=True)
                remote_coordinator.enqueue(root, {'id': jid, 'kind': 'prepare', 'task': jid, 'payload': {}})
            coordinator = remote_coordinator.Coordinator(root)
            coordinator.update('job2', state='running')
            remote_api.dispatch(root, {'action': 'cancel', 'job_id': 'job1'})
            remote_api.dispatch(root, {'action': 'cancel', 'job_id': 'job2'})
            jobs = {item['id']: item for item in coordinator.jobs()}
            self.assertEqual(jobs['job1']['state'], 'interrupted')
            self.assertEqual(jobs['job2']['state'], 'running')
            self.assertTrue((root / 'jobs/job2/cancel').exists())
            self.assertFalse((root / 'jobs/job1/cancel').exists())


class RemoteHealthTests(unittest.TestCase):
    def test_health_checks_selected_image_only_after_rootless_check(self):
        from unittest.mock import patch
        from mineval.remote_api import dispatch
        with tempfile.TemporaryDirectory() as temp, patch('mineval.sandbox.DockerSandbox') as sandbox:
            sandbox.return_value.image.return_value = {'Id': 'sha256:fixture'}
            result = dispatch(temp, {'action': 'health', 'image': 'local:test'})
            self.assertEqual(result, {'ready': True, 'image_id': 'sha256:fixture'})
            sandbox.return_value.check.side_effect = ValueError('not rootless')
            self.assertFalse(dispatch(temp, {'action': 'health', 'image': 'local:test'})['ready'])
            self.assertEqual(sandbox.return_value.image.call_count, 1)


class PreparationAuthTests(HistoryFixture):
    def test_preparation_passes_credentials_only_to_codex(self):
        from unittest.mock import patch
        from mineval.workflows import Workflows
        class Stop(Exception): pass
        class Progress:
            def __init__(self, *args): pass
            def state(self): return {}
            def cleanup(self, value): return True
            def run(self, stage, inputs, operation, validator, **kwargs): return operation()
        for adapter_name, expected in (('fake', None), ('codex', '/model-only-auth')):
            flow = Workflows(self.store, {'preparation_image': 'fixture'}, auth_file='/model-only-auth')
            payload = {'model_settings': {'preparation': {'adapter': adapter_name, 'model': 'fixture', 'effort': 'low'}}}
            with patch('mineval.checkpoints.PreparationCheckpoints', Progress), \
                    patch('mineval.service.session', side_effect=Stop) as session:
                with self.assertRaises(Stop):
                    flow.prepare('testjob', 'example', payload)
            self.assertEqual(session.call_args.kwargs['auth_file'], expected)
