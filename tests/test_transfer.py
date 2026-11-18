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
        with self.assertRaisesRegex(ValueError, 'Unsafe symbolic link'):
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
    def test_imports_sealed_architecture_evidence_from_existing_remote_attempt(self):
        from unittest.mock import patch
        from mineval import architecture
        from mineval.artifacts import inventory, write_json

        task = 'example'
        version = self.freeze(task, self.review)['version']
        revision = self.store.inspect_revision(task, version)
        options = {'image': 'local:test', 'model': 'reviewer', 'effort': 'low'}
        observations = {'observations': [{'description': 'Answer function', 'evidence': ['app.py']}]}
        with patch('mineval.architecture._infer', return_value=observations):
            benchmark.run(self.store, task, version, run_id='peer', adapter=FakeAdapter(model='peer'),
                          sandbox=SimulatedDocker())
            architecture.record(self.store, task, 'peer', **options)
            remote_root = self.root / 'remote'
            shutil.copytree(self.store.root, remote_root)
            remote = Store(remote_root)
            benchmark.run(remote, task, version, run_id='remote-run', adapter=FakeAdapter(effort='medium'),
                          sandbox=SimulatedDocker())
            record = architecture.record(remote, task, 'remote-run', **options)
        ranking = {'ranking': [{'candidates': ['C1', 'C2'], 'explanation': 'Same interface',
                                'evidence': [{'candidate': 'C1', 'path': 'app.py'}]}], 'uncertainty': ''}
        with patch('mineval.architecture._infer', return_value=ranking):
            comparison = architecture.compare(remote, task, version, **options)
        job = {'task': task, 'payload': {'run_id': 'remote-run', 'evaluation_id': 'eval1',
               'revision': revision, 'configuration': {'model': 'fake', 'effort': 'medium', 'adapter': 'fake'}}}
        paths = ('architecture/records/remote-run', 'architecture/comparisons/' + comparison['comparison_id'])
        source_record = remote.task(task) / paths[0] / 'record.json'
        source_record.chmod(0o600)
        source_record.write_text(json.dumps(record | {'run_id': 'wrong-run'}))
        with self.assertRaisesRegex(ValueError, 'artifact hash mismatch'):
            transfer.import_benchmark(self.store, remote.root, job)
        self.assertFalse((self.store.task(task) / 'runs/remote-run.json').exists())
        write_json(source_record, record)
        for _ in range(2):
            transfer.import_benchmark(self.store, remote.root, job)
            for path in paths:
                self.assertEqual(inventory(self.store.task(task) / path), inventory(remote.task(task) / path))

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


class DeltaTransferTests(HistoryFixture):
    def delta(self):
        import os
        from mineval.artifacts import inventory, read_json
        large = self.store.task('example') / 'private/large-fixture.bin'
        large.write_bytes(os.urandom(1024 * 1024))
        job = {'id': 'job1', 'kind': 'prepare', 'task': 'example', 'payload': {}}
        source = self.root / 'prep-delta'
        transfer.preparation_input(self.store, job, source, {})
        selected = source / 'data'
        (selected / 'tasks/example/new-evidence').write_text('new')
        full = inventory(selected)
        known = transfer.local_inventory(self.store, 'example', 'job1')
        archive, receipt = self.root / 'delta.tar.gz', self.root / 'delta-receipt.json'
        descriptor = transfer.create_delta(selected, archive, receipt, known, 'example')
        self.assertLess(archive.stat().st_size, large.stat().st_size // 4)
        destination = self.root / 'received'
        subset = transfer.verify(archive, receipt, destination)
        return destination, descriptor, subset, full, large

    def test_delta_roundtrip_and_unchanged_resume(self):
        from mineval.artifacts import inventory
        destination, descriptor, subset, full, large = self.delta()
        from mineval.artifacts import write_json
        marker = self.store.root / 'remote-imports/example.json'
        write_json(marker, {'job_id': 'job1'})
        self.assertTrue(transfer.local_inventory(self.store, 'example', 'job1'))
        with self.assertRaisesRegex(ValueError, 'Another remote'):
            transfer.local_inventory(self.store, 'example', 'job2')
        transfer.reconstruct_delta(destination, descriptor, subset, self.store, 'example', 'job1')
        self.assertEqual(inventory(destination), full)
        self.assertEqual((destination / 'tasks/example/private/large-fixture.bin').read_bytes(), large.read_bytes())

    def test_delta_rejects_unsafe_mismatch_missing_and_symlink(self):
        from copy import deepcopy
        destination, descriptor, subset, full, large = self.delta()
        for name in ('../escape', 'tasks/other/file'):
            bad = deepcopy(descriptor)
            bad['full_inventory'][name] = {'sha256': '0' * 64, 'executable': False}
            with self.assertRaises(ValueError):
                transfer.reconstruct_delta(destination, bad, subset, self.store, 'example', 'job1')
        bad = deepcopy(descriptor)
        key = next(iter(subset['files']))
        bad['full_inventory'][key]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'disagrees'):
            transfer.reconstruct_delta(destination, bad, subset, self.store, 'example', 'job1')
        original = large.read_bytes()
        large.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed or is missing'):
            transfer.reconstruct_delta(destination, descriptor, subset, self.store, 'example', 'job1')
        large.write_bytes(original)
        large.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'changed or is missing'):
            transfer.reconstruct_delta(destination, descriptor, subset, self.store, 'example', 'job1')
        large.unlink()
        with self.assertRaisesRegex(ValueError, 'changed or is missing'):
            transfer.reconstruct_delta(destination, descriptor, subset, self.store, 'example', 'job1')
        large.symlink_to(self.root / 'elsewhere')
        with self.assertRaises(ValueError):
            transfer.reconstruct_delta(destination, descriptor, subset, self.store, 'example', 'job1')
        large.unlink()
        large.write_bytes(original)


class PreparationImportTests(HistoryFixture):
    def test_checkpoint_changes_reconcile_but_concurrent_edits_conflict(self):
        from mineval.artifacts import write_json
        job = {'id': 'job1', 'kind': 'prepare', 'task': 'example', 'payload': {}}
        progress = self.store.task('example') / 'preparation-progress.json'
        write_json(progress, {'old': True})
        snapshot = self.root / 'prep-input'
        transfer.preparation_input(self.store, job, snapshot, {})
        baseline = json.loads((snapshot / 'baseline.json').read_text())
        remote_progress = snapshot / 'data/tasks/example/preparation-progress.json'
        write_json(remote_progress, {'new': True})
        transfer.import_preparation(self.store, snapshot / 'data', job, baseline)
        self.assertEqual(json.loads(progress.read_text()), {'new': True})
        write_json(progress, {'local': True})
        with self.assertRaisesRegex(ValueError, 'task changed'):
            transfer.import_preparation(self.store, snapshot / 'data', job, baseline)

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
    def test_retry_reconciles_download_before_retrying_remote_job(self):
        from unittest.mock import patch
        from mineval.workflows import Workflows
        flow = Workflows(self.store, {'vps': {'host': 'test@example.test'}})
        flow.enqueue('prepare', [{'task_id': 'example'}], execution_host='vps')
        job = flow.jobs()[0]
        flow.update(job['id'], state='interrupted')
        directory = self.store.root / 'remote-outbox' / job['id']
        directory.mkdir(parents=True)
        (directory / 'download.json').write_text('{}')
        (directory / 'download.partial').write_bytes(b'partial')
        with patch('mineval.remote.SSHClient', side_effect=AssertionError('must reconcile first')):
            flow.retry(job['id'])
        resumed = flow.jobs()[0]
        self.assertEqual((resumed['state'], resumed['stage']), ('queued', 'resuming remote import'))

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
        def must_not_recompute():
            self.fail('Resuming must retain the original descriptor')
        info, partial = fetch_resumable(client, 'job1', self.root, known_inventory=must_not_recompute)
        self.assertEqual(client.starts, 1)
        self.assertEqual(info['token'], 'export1')
        self.assertEqual(partial.read_bytes(), content)

    def test_remote_dispatch_executes_and_imports_fake_attempt(self):
        from unittest.mock import patch
        from mineval import remote_api, remote_coordinator, remote_dispatch
        from mineval.workflows import Workflows
        import threading
        self.fixture_generated_cases = True
        self.fixture_evaluation_config = {'image': 'local:test', 'network': 'none', 'grading_type': 'isolated-argv-v1',
            'commands': [{'name': 'answer-regression', 'kind': 'tests',
                          'argv': ['python3', '-c', 'from app import answer; print(answer())']}]}
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
                    if request['action'] == 'upload-finish' and not Client.submissions:
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
                config.write_text(json.dumps(self.fixture_evaluation_config))
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
        descriptors = list((self.store.root / 'remote-outbox/job1/export-descriptors').glob('*.json'))
        self.assertEqual(len(descriptors), 1)
        descriptor = json.loads(descriptors[0].read_text())
        self.assertIn('full_inventory', descriptor['delta'])
        self.assertIn('archive_sha256', descriptor['receipt'])
        self.assertEqual(descriptors[0].stat().st_mode & 0o222, 0)
        self.assertEqual({j['state'] for j in flow.jobs()}, {'completed'})
        self.assertEqual(self.store.inspect_run('example', 'local-run')['status'], 'completed')
        self.assertEqual(self.store.inspect_run('example', 'dispatch-run')['status'], 'completed')
        self.assertEqual(self.store.inspect_evaluation('example', 'dispatch-eval')['run_id'], 'dispatch-run')
        from mineval.memory import Memory
        self.assertTrue(any(item['body'] == ['Evidence-backed review'] for item in Memory(self.store).history('fake'))) 
        self.assertTrue(list((self.store.root / 'resource-usage').glob('job1-*.json')))


class RetryTests(HistoryFixture):
    def test_remote_reevaluation_allocates_once_and_keeps_saved_run(self):
        from unittest.mock import patch
        from mineval import evaluation, remote_api, remote_coordinator
        from mineval.artifacts import digest, write_json
        from mineval.workflows import Workflows
        from test_evaluation import EvaluationRunner
        self.fixture_generated_cases = True
        self.fixture_evaluation_config = {'image': 'local:test', 'network': 'none', 'grading_type': 'isolated-argv-v1',
            'commands': [{'name': 'answer-regression', 'kind': 'tests',
                          'argv': ['python3', '-c', 'from app import answer; print(answer())']}]}
        version = self.freeze('example', self.review)['version']
        revision = self.store.inspect_revision('example', version)
        benchmark.run(self.store, 'example', version, run_id='saved', adapter=FakeAdapter(effort='medium'), sandbox=SimulatedDocker())
        config = self.root / 'reeval-config.json'
        write_json(config, self.fixture_evaluation_config)
        evaluation.evaluate(self.store, 'example', 'saved', config, evaluation_id='old-eval', sandbox=EvaluationRunner())
        old = self.store.task('example') / 'evaluations/old-eval.json'
        before = digest(old)
        payload = {'run_id': 'saved', 'evaluation_id': 'old-eval', 'phase': 'review', 'revision': revision,
                   'execution_host': 'vps', 'configuration': {'model': 'fake', 'effort': 'medium', 'adapter': 'fake'}}
        job = {'id': 'job1', 'kind': 'benchmark', 'task': 'example', 'payload': payload}
        worker = self.root / 'reeval-worker'
        input_dir = worker / 'jobs/job1/input'
        input_dir.mkdir(parents=True)
        shutil.copytree(self.store.root, input_dir / 'data')
        remote_coordinator.enqueue(worker, job)
        remote_coordinator.Coordinator(worker).update('job1', state='completed')
        flow = Workflows(self.store, {'vps': {'host': 'test@example.test'}})
        with flow.db() as db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)', ('job1', 'benchmark', 'example', json.dumps(payload),
                       'completed', 'completed', '', 1, 1, 'dedup'))
        requests = []
        class Client:
            def __init__(self, host): pass
            def request(self, request, timeout=60):
                requests.append(request)
                return remote_api.dispatch(worker, request)
        with patch('mineval.workflows.SSHClient', Client, create=True), \
             patch('mineval.remote.SSHClient', Client), patch('mineval.remote_coordinator.start'), \
             patch('mineval.benchmark.run', side_effect=AssertionError('benchmark must not rerun')):
            flow.retry('job1', reevaluate=True)
            retried = flow.jobs()[0]
            again = remote_api.dispatch(worker, requests[0])
        self.assertEqual(retried['payload']['run_id'], 'saved')
        self.assertEqual(retried['payload']['phase'], 'evaluation')
        self.assertNotEqual(retried['payload']['evaluation_id'], 'old-eval')
        self.assertEqual(retried['payload']['evaluation_id'], again['payload']['evaluation_id'])
        self.assertEqual(retried['payload']['internal_evaluation_ids'], ['old-eval'])
        self.assertEqual(digest(old), before)
        self.assertEqual(requests[0]['action'], 'retry')
        self.assertEqual(requests[0]['expected_evaluation_id'], 'old-eval')
        self.assertEqual(self.store.inspect_run('example', 'saved')['status'], 'completed')
        with self.assertRaisesRegex(ValueError, 'combine'):
            flow.retry('job1', reevaluate=True, new_attempt=True)

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

class ResumableProtocolTests(unittest.TestCase):
    def test_upload_recovers_partial_and_lost_queue_response_and_rejects_changed_bundle(self):
        import base64
        from unittest.mock import patch
        from mineval import remote_api, remote_coordinator
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            source.mkdir()
            (source / 'job.json').write_text(json.dumps({'id': 'job1', 'kind': 'prepare', 'task': 'task1', 'payload': {}}))
            (source / 'settings.json').write_text('{}')
            (source / 'data').write_bytes(__import__('os').urandom(700000))
            archive, receipt = root / 'archive', root / 'receipt'
            transfer.create(source, archive, receipt)
            data, manifest = archive.read_bytes(), json.loads(receipt.read_text())
            worker = root / 'worker'
            request = {'action': 'upload-start', 'job_id': 'job1', 'size': len(data), 'receipt': manifest}
            with patch('mineval.remote_coordinator.ensure_running'):
                self.assertEqual(remote_api.dispatch(worker, request)['offset'], 0)
                remote_api.dispatch(worker, {'action': 'upload-chunk', 'job_id': 'job1', 'offset': 0,
                                            'data': base64.b64encode(data[:400000]).decode()})
                self.assertEqual(remote_api.dispatch(worker, request)['offset'], 400000)
                changed = request | {'receipt': manifest | {'archive_sha256': '0' * 64}}
                with self.assertRaisesRegex(ValueError, 'identity conflict'):
                    remote_api.dispatch(worker, changed)
                remote_api.dispatch(worker, {'action': 'upload-chunk', 'job_id': 'job1', 'offset': 400000,
                                            'data': base64.b64encode(data[400000:]).decode()})
                # Crash after verified publication but before queue insertion.
                with patch('mineval.remote_coordinator.enqueue', side_effect=OSError('crash')):
                    with self.assertRaises(OSError):
                        remote_api.dispatch(worker, {'action': 'upload-finish', 'job_id': 'job1'})
                self.assertTrue(remote_api.dispatch(worker, request)['complete'])
                remote_api.dispatch(worker, {'action': 'upload-finish', 'job_id': 'job1'})
                remote_api.dispatch(worker, {'action': 'upload-finish', 'job_id': 'job1'})
                self.assertEqual(len(remote_coordinator.Coordinator(worker).jobs()), 1)

    def test_refresh_preserves_legacy_and_resumes_new_delta(self):
        import base64
        from mineval.remote import RemoteError
        from mineval.remote_dispatch import fetch_resumable
        from mineval.artifacts import write_json
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {'state': 'failed', 'updated': 1, 'payload': {}}
            legacy = {'token': 'old', 'size': 10, 'state': state, 'receipt': {}}
            write_json(root / 'download.json', legacy)
            (root / 'download.partial').write_bytes(b'old-bytes')
            class Client:
                starts = 0
                requests = []
                def request(inner, request, timeout=60):
                    inner.requests.append(request)
                    if request['action'] == 'fetch-start':
                        inner.starts += 1
                        self.assertEqual(request['known_inventory'], {'known': 'inventory'})
                        return {'token': 'new', 'size': 3, 'state': state, 'receipt': {},
                                'delta': {'schema_version': 1, 'full_inventory': {}}}
                    if len(inner.requests) == 2:
                        raise RemoteError('connection lost')
                    return {'offset': request['offset'], 'total': 3, 'data': base64.b64encode(b'new').decode()}
            client = Client()
            with self.assertRaises(RemoteError):
                fetch_resumable(client, 'job1', root, state, known_inventory=lambda: {'known': 'inventory'}, refresh_export=True)
            backups = list((root / 'export-backups').iterdir())
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads((backups[0] / 'download.json').read_text()), legacy)
            self.assertEqual((backups[0] / 'download.partial').read_bytes(), b'old-bytes')
            result, partial = fetch_resumable(client, 'job1', root, state, refresh_export=True)
            self.assertEqual((result['token'], partial.read_bytes(), client.starts), ('new', b'new', 1))
            self.assertEqual(len(list((root / 'export-backups').iterdir())), 1)
            self.assertEqual([r['action'] for r in client.requests], ['fetch-start', 'fetch-chunk', 'fetch-chunk'])

    def test_refresh_switch_recovers_after_interrupted_unlink(self):
        from unittest.mock import patch
        from mineval.remote_dispatch import fetch_resumable
        from mineval.artifacts import write_json
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {'state': 'failed', 'updated': 1, 'payload': {}}
            write_json(root / 'download.json', {'token': 'old', 'size': 10, 'state': state, 'receipt': {}})
            (root / 'download.partial').write_bytes(b'old')
            with patch('mineval.remote_dispatch._finish_export_switch', side_effect=[None, RuntimeError('crash')]):
                with self.assertRaises(RuntimeError):
                    fetch_resumable(None, 'job1', root, state, refresh_export=True)
            self.assertTrue((root / 'export-switch.json').exists())
            (root / 'download.json').unlink()
            class Client:
                def request(inner, request, timeout=60):
                    self.assertEqual(request['action'], 'fetch-start')
                    return {'token': 'new', 'size': 0, 'state': state, 'receipt': {}}
            result, partial = fetch_resumable(Client(), 'job1', root, state)
            self.assertEqual(result['token'], 'new')
            self.assertEqual(partial.read_bytes(), b'')
            self.assertFalse((root / 'export-switch.json').exists())
            self.assertEqual(next((root / 'export-backups').iterdir()).joinpath('download.partial').read_bytes(), b'old')

    def test_stale_attempt_discards_partial_export(self):
        import base64
        from mineval.remote_dispatch import fetch_resumable
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = {'state': 'interrupted', 'updated': 1, 'payload': {'run_id': 'old', 'evaluation_id': 'one'}}
            new = {'state': 'completed', 'updated': 2, 'payload': {'run_id': 'new', 'evaluation_id': 'two'}}
            (root / 'download.json').write_text(json.dumps({'token': 'old', 'size': 3, 'state': old}))
            (root / 'download.partial').write_bytes(b'bad')
            class Client:
                def request(self, request, timeout=60):
                    if request['action'] == 'fetch-start':
                        return {'token': 'new', 'size': 3, 'state': new, 'receipt': {}}
                    self_test.assertEqual(request['offset'], 0)
                    return {'offset': 0, 'total': 3, 'data': base64.b64encode(b'new').decode()}
            self_test = self
            info, path = fetch_resumable(Client(), 'job1', root, new)
            self.assertEqual((info['token'], path.read_bytes()), ('new', b'new'))

    def test_expired_exports_cleaned_without_removing_fresh_ones(self):
        import os, time
        from mineval.remote_api import cleanup_exports, EXPORT_TTL
        with tempfile.TemporaryDirectory() as temp:
            exports = Path(temp) / 'jobs/job1/exports'
            exports.mkdir(parents=True)
            old, fresh = exports / 'old.tar.gz', exports / 'fresh.tar.gz'
            old.touch(); fresh.touch()
            os.utime(old, (time.time() - EXPORT_TTL - 10,) * 2)
            cleanup_exports(temp)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())

    def test_vps_reservations_follow_each_jobs_limits(self):
        from mineval.remote_coordinator import Coordinator
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'settings.json').write_text(json.dumps({'resources': {'reservation_percent': 50}}))
            coordinator = Coordinator(root)
            for jid, cpu, ram in [('small', 1, 1024), ('large', 4, 8192)]:
                config = root / 'jobs' / jid / 'input/data/tasks/task1/versions/v1/verify-config.json'
                config.parent.mkdir(parents=True)
                config.write_text(json.dumps({'limits': {'cpus': cpu, 'memory_mb': ram}}))
                job = {'id': jid, 'task': 'task1', 'payload': {'selection': {'version': 'v1'}}}
                self.assertEqual(coordinator.resource_request(job, 'benchmark'), (cpu * .5, ram * .5))
