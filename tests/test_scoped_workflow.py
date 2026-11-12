import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mineval.cli import _main
from mineval.preparation import Store
from mineval.workflows import Workflows


class ScopedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'store')
        self.flow = Workflows(self.store, {'resources': {'admission_interval_seconds': 10}})

    def insert(self, jid, state='queued', host='local'):
        with self.flow.db() as db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (jid, 'prepare', jid, json.dumps({'execution_host': host}),
                        state, state, '', 1, 1, jid))

    def states(self):
        return {job['id']: job['state'] for job in self.flow.jobs()}

    def test_local_execution_and_recovery_are_scoped(self):
        self.insert('selected')
        self.insert('other-queued')
        self.insert('other-running', 'running')
        executed = []
        def run(job, phase, usage):
            executed.append(job['id'])
            self.flow.update(job['id'], state='completed')
        with patch.object(self.flow, '_measured_stage', side_effect=run):
            self.flow.work(job_id='selected')
        self.assertEqual(executed, ['selected'])
        self.assertEqual(self.states(), {'selected': 'completed', 'other-queued': 'queued', 'other-running': 'running'})
        self.flow.work(job_id='other-running')
        self.assertEqual(self.states()['other-running'], 'interrupted')
        self.assertEqual(self.states()['other-queued'], 'queued')

    def test_remote_dispatch_is_scoped(self):
        self.insert('selected', host='vps')
        self.insert('other', 'running', host='vps')
        dispatched = []
        def run(flow, job, stop):
            dispatched.append(job['id'])
            flow.update(job['id'], state='completed')
        with patch('mineval.remote_dispatch.run_job', side_effect=run):
            self.flow.work(job_id='selected')
        self.assertEqual(dispatched, ['selected'])
        self.assertEqual(self.states(), {'selected': 'completed', 'other': 'running'})

    def test_unknown_job_is_rejected_before_workers_start(self):
        self.insert('other', 'running')
        with patch('mineval.remote_dispatch.dispatch') as dispatch:
            with self.assertRaisesRegex(ValueError, 'Unknown workflow job: missing'):
                self.flow.work(job_id='missing')
        dispatch.assert_not_called()
        self.assertEqual(self.states()['other'], 'running')

    def test_local_reevaluation_reuses_run_and_skips_benchmark_phase(self):
        self.insert('selected', 'completed')
        revision = {'version': 'v1'}
        config = {'model': 'fake', 'effort': 'medium', 'adapter': 'fake'}
        payload = {'run_id': 'saved', 'evaluation_id': 'old', 'revision': revision,
                   'configuration': config, 'phase': 'review'}
        with self.flow.db() as db:
            db.execute('UPDATE jobs SET kind=?,payload=? WHERE id=?', ('benchmark', json.dumps(payload), 'selected'))
        path = self.store.task('selected') / 'runs/saved.json'
        path.parent.mkdir(parents=True)
        path.write_text('{}')
        record = {'status': 'completed', 'output_available': True, 'schema_version': 2,
                  'prepared_revision': revision, 'model': {'name': 'fake', 'reasoning_effort': 'medium'},
                  'harness': {'name': 'fake', 'adapter_version': 1}}
        phases = []
        with patch.object(self.store, 'inspect_run', return_value=record), \
             patch.object(self.flow, 'benchmark', side_effect=lambda jid, task, p, *, phase: phases.append(phase)), \
             patch('mineval.resources.admits', return_value=True):
            self.flow.retry('selected', reevaluate=True)
            retried = self.flow.jobs()[0]
            self.assertEqual(retried['payload']['run_id'], 'saved')
            self.assertNotEqual(retried['payload']['evaluation_id'], 'old')
            self.assertEqual(retried['payload']['internal_evaluation_ids'], ['old'])
            self.flow.work(job_id='selected')
        self.assertEqual(phases, ['evaluation', 'review'])
        self.assertEqual(self.states()['selected'], 'completed')

    def test_reevaluation_rejects_nonbenchmark_and_missing_run(self):
        self.insert('selected', 'completed')
        with self.assertRaisesRegex(ValueError, 'benchmark'):
            self.flow.retry('selected', reevaluate=True)
        with self.flow.db() as db:
            db.execute('UPDATE jobs SET kind=?,payload=? WHERE id=?',
                       ('benchmark', json.dumps({'run_id': 'missing'}), 'selected'))
        with self.assertRaisesRegex(ValueError, 'usable saved run'):
            self.flow.retry('selected', reevaluate=True)
        output = io.StringIO()
        with patch('mineval.configuration.load', return_value={}), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit):
                _main(['--data', str(self.store.root), 'workflow', 'retry', 'selected', '--reevaluate', '--new-attempt'])
        self.assertIn('not allowed with argument', output.getvalue())

    def test_refresh_is_scoped_and_does_not_execute_local_work(self):
        self.insert('selected', 'interrupted', host='vps')
        self.insert('other', 'running', host='vps')
        self.insert('local', 'running')
        processed = []
        def remote(flow, job, stop, refresh):
            processed.append((job['id'], refresh))
        with patch('mineval.remote_dispatch.run_job', side_effect=remote), \
             patch.object(self.flow, '_local_work', side_effect=AssertionError('local work')):
            self.flow.work(job_id='selected', refresh_export=True)
        self.assertEqual(processed, [('selected', True)])
        self.assertEqual(self.states()['local'], 'running')
        self.assertEqual(self.states()['other'], 'running')

    def test_refresh_does_not_submit_a_nonterminal_remote_job(self):
        from mineval.remote_dispatch import run_job
        import threading
        self.flow.settings['vps'] = {'host': 'test@example.test'}
        self.insert('selected', 'interrupted', host='vps')
        (self.store.root / 'remote-outbox/selected').mkdir(parents=True)
        actions = []
        class Client:
            def __init__(self, host): pass
            def request(self, request, **kwargs):
                actions.append(request['action'])
                return {'state': 'ready'}
        with patch('mineval.remote_dispatch.SSHClient', Client), \
             patch('mineval.remote_dispatch.upload_resumable', side_effect=AssertionError('must not submit')):
            run_job(self.flow, self.flow.jobs()[0], threading.Event(), True)
        self.assertEqual(actions, ['status'])
        self.assertEqual(self.states()['selected'], 'interrupted')

    def test_refresh_requires_job_id(self):
        with self.assertRaisesRegex(ValueError, 'requires a job ID'):
            self.flow.work(refresh_export=True)
        output = io.StringIO()
        with patch('mineval.configuration.load', return_value={}), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit):
                _main(['--data', str(self.store.root), 'workflow', 'work', '--refresh-export'])
        self.assertIn('--refresh-export requires --job-id', output.getvalue())

    def test_cli_accepts_selector_and_reports_unknown_job(self):
        output = io.StringIO()
        with patch('mineval.configuration.load', return_value={}), contextlib.redirect_stderr(output):
            code = _main(['--data', str(self.store.root), 'workflow', 'work', '--job-id', 'missing'])
        self.assertEqual(code, 125)
        self.assertIn('Unknown workflow job: missing', output.getvalue())
