"""Historical workflows: real saved Git/artifacts, simulated isolated agents only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from mineval import benchmark, evaluation
from mineval.agents import CodexAdapter, FakeAdapter
from mineval.artifacts import read_json, write_json
from mineval.memory import Memory
from mineval.preparation import Store
from mineval.workflows import Workflows, matches, tasks
from mineval.observation import Pages, make_server
from mineval.performance import summaries
from test_agents import SimulatedDocker
import test_evaluation as fixtures
from test_evaluation import EvaluationRunner
from test_preparation import HistoryFixture, FakeRunner, step
from mineval.verification import diagnose
from review_helpers import ReportAgent, plan, report


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'store')
        self.memory = Memory(self.store)

    def append(self, n):
        return Memory(self.store).append('model/a', ['Observed ' + str(n)],
            [{'task_id': str(n), 'model': {'name': 'model/a', 'reasoning_effort': 'high'}}], str(n))

    def test_concurrent_append_claim_commit_preserves_every_source(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.append, range(32)))
        self.assertEqual(len(self.memory.history('model/a')), 32)
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: Memory(self.store).claim('model/a'), range(8)))
        claim, = [r for r in claims if r]
        inputs = self.memory.inspect_claim(claim['reservation_id'])['inputs']
        self.assertEqual([i['id'] for i in inputs], [r['id'] for r in self.memory.history('model/a')[:2]])
        self.append(32)  # Concurrent appends are never folded away or lost.
        def commit(_):
            return Memory(self.store).commit(claim['reservation_id'], ['Combined evidence'])
        with ThreadPoolExecutor(max_workers=8) as pool:
            committed = list(pool.map(commit, range(8)))
        self.assertEqual(len({r['id'] for r in committed}), 1)
        self.assertEqual(len(self.memory.history('model/a')), 32)
        self.assertEqual(len(self.memory.history('model/a', archive=True)), 34)
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.memory.commit(claim['reservation_id'], ['Changed'])
        while len(self.memory.history('model/a')) >= 24:
            c = self.memory.claim('model/a')
            self.memory.commit(c['reservation_id'], ['Preserved older evidence'])
        active = self.memory.history('model/a')
        sources = [s['task_id'] for r in active for s in r['sources']]
        self.assertEqual(set(sources), {str(x) for x in range(33)})
        self.assertEqual(len(sources), 33)

    def test_failed_compaction_releases_claim_without_losing_observations(self):
        from mineval.model_review import compact
        for n in range(24):
            self.append(n)
        before = self.memory.history('model/a')
        with patch('mineval.model_review.infer', side_effect=ValueError('fixture failure')):
            with self.assertRaises(ValueError):
                compact(self.store, 'model/a', image='local:test')
        self.assertEqual(self.memory.history('model/a'), before)
        self.assertIsNotNone(self.memory.claim('model/a'))

    def test_expired_claim_cannot_commit_reclaimed_inputs_and_duplicates_are_idempotent(self):
        for n in range(24):
            self.append(n)
        claim = self.memory.claim('model/a', ttl=1)
        with self.memory.connect() as db:
            db.execute('UPDATE reservations SET expires=? WHERE id=?', (time.time() - 1, claim['reservation_id']))
        replacement = self.memory.claim('model/a')
        self.assertIsNotNone(replacement)
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.memory.commit(claim['reservation_id'], ['Stale'])
        self.memory.commit(replacement['reservation_id'], ['Current'])
        self.append(0)
        self.assertEqual(len(self.memory.history('model/a', archive=True)), 25)
        self.assertEqual(Memory(self.store).inspect_claim(replacement['reservation_id'])['state'], 'committed')


class WorkflowTests(HistoryFixture):
    def setUp(self):
        super().setUp()
        catalog = patch('mineval.codex_models.Catalog.get', return_value={'models': [], 'error': None})
        catalog.start(); self.addCleanup(catalog.stop)
        self.version = self.freeze('example', self.review)['version']
        self.config = self.root / 'evaluation.json'
        write_json(self.config, {'image': 'local:test', 'network': 'none', 'commands': [
            {'name': 'regression', 'kind': 'tests', 'argv': ['sh', '-c', 'PYTHONPATH=. python3 tests/test_answer.py']}]})
    saved = fixtures.EvaluationTests.saved
    assess = fixtures.EvaluationTests.assess
    def flow(self):
        return Workflows(self.store, {'evaluation_config': str(self.config), 'evaluator_agent': False})

    def test_exact_effort_revision_exclusion_and_explicit_rerun(self):
        flow = self.flow()
        selection = [{'task_id': 'example', 'version': self.version}]
        config = [{'model': 'sample', 'effort': 'high', 'adapter': 'fake'}]
        first = flow.enqueue('benchmark', selection, config)
        again = flow.enqueue('benchmark', selection, config)
        self.assertEqual(again[0]['id'], first[0]['id'])
        with patch('mineval.workflows.DockerSandbox', return_value=EvaluationRunner()), \
             patch('mineval.benchmark.run_agent', side_effect=lambda *a, **kw: self._run_agent(*a, **kw)):
            flow.work()
        job = flow.jobs()[0]
        self.assertEqual(job['state'], 'completed', job)
        record = self.store.inspect_run('example', job['payload']['run_id'])
        self.assertEqual(record['model'], {'name': 'sample', 'reasoning_effort': 'high'})
        self.assertTrue(matches(record, record['prepared_revision'], config[0]))
        self.assertEqual(len(Memory(self.store).history('sample')), 1)
        self.assertEqual(flow.enqueue('benchmark', selection, config)[0]['state'], 'skipped')
        self.assertEqual(flow.enqueue('benchmark', selection, config, exclude=False)[0]['state'], 'queued')
        self.assertEqual(flow.enqueue('benchmark', selection, [config[0] | {'effort': 'low'}])[0]['state'], 'queued')
        newer = self.freeze('example', self.review)['version']
        self.assertEqual(flow.enqueue('benchmark', [{'task_id': 'example', 'version': newer}], config)[0]['state'], 'queued')

    def test_evaluation_uses_queued_model_and_effort(self):
        flow = Workflows(self.store, {'evaluation_config': str(self.config),
            'evaluation_model': 'eval-selected', 'evaluation_effort': 'ultra',
            'qualitative_review': False})
        flow.enqueue('benchmark', [{'task_id': 'example', 'version': self.version}],
                     [{'model': 'candidate', 'effort': 'low', 'adapter': 'codex'}])
        job = flow.jobs()[0]
        flow.settings['evaluation_model'] = 'changed-after-queue'
        with patch('mineval.workflows.benchmark.run', return_value={'output_available': True, 'status': 'completed'}), \
             patch('mineval.workflows.evaluation.evaluate', return_value={'evaluation_id': 'fixture'}) as evaluate, \
             patch('mineval.memory.record_evaluation'):
            flow.benchmark(job['id'], 'example', job['payload'])
        evaluator = evaluate.call_args.kwargs['adapter']
        self.assertEqual((evaluator.model, evaluator.effort), ('eval-selected', 'ultra'))

    def _run_agent(self, *args, **kwargs):
        from mineval.agents import run_agent
        kwargs['sandbox'] = SimulatedDocker()
        return run_agent(*args, **kwargs)

    def test_failed_evaluation_retry_reuses_run_and_published_evaluation(self):
        flow = self.flow()
        flow.enqueue('benchmark', [{'task_id': 'example', 'version': self.version}],
                     [{'model': 'sample', 'effort': 'high', 'adapter': 'fake'}])
        with patch('mineval.benchmark.run_agent', side_effect=self._run_agent), \
             patch('mineval.workflows.evaluation.evaluate', side_effect=ValueError('fixture failure')):
            flow.work()
        job = flow.jobs()[0]
        self.assertEqual(job['state'], 'failed')
        run_id = job['payload']['run_id']
        before = self.store.inspect_run('example', run_id)
        flow.retry(job['id'])
        with patch('mineval.workflows.benchmark.run', side_effect=AssertionError('must reuse saved run')), \
             patch('mineval.workflows.DockerSandbox', return_value=EvaluationRunner()):
            flow.work()
        self.assertEqual(flow.jobs()[0]['state'], 'completed', flow.jobs())
        self.assertEqual(self.store.inspect_run('example', run_id), before)

    def test_interrupted_worker_does_not_rerun_uncertain_attempt(self):
        flow = self.flow()
        flow.enqueue('benchmark', [{'task_id': 'example', 'version': self.version}],
                     [{'model': 'sample', 'effort': 'high', 'adapter': 'fake'}])
        job = flow.jobs()[0]
        flow.update(job['id'], state='running', stage='benchmark')
        flow.work()
        self.assertEqual(flow.jobs()[0]['state'], 'interrupted')
        flow.retry(job['id'])
        self.assertEqual(flow.jobs()[0]['state'], 'queued')

    def test_independent_preparation_rejection_does_not_block_pool(self):
        # Use new tasks so prepared tasks are not accidentally re-cleaned.
        self.store.register(self.candidate | {'task_id': 'first'})
        self.store.register(self.candidate | {'task_id': 'second'})
        flow = self.flow()
        flow.enqueue('prepare', [{'task_id': 'first'}, {'task_id': 'second'}])
        seen = []
        def prepare(jid, tid, payload):
            seen.append(tid)
            if tid == 'first':
                raise ValueError('rejected preparation')
        with patch.object(flow, 'prepare', side_effect=prepare):
            flow.work()
        self.assertEqual(seen, ['first', 'second'])
        self.assertEqual({j['state'] for j in flow.jobs()}, {'completed', 'failed'})

    def test_legacy_category_model_memory_and_common_condition_coverage(self):
        self.saved('one'); self.assess('one', 'first')
        self.saved('two'); self.assess('two', 'second')
        report = summaries(self.store)
        self.assertEqual(report['models'][0]['overall']['evaluated'], 1)
        self.assertEqual(report['models'][0]['categories']['uncategorized']['evaluated'], 1)
        self.assertIsNone(report['models'][0]['category_average'])
        memory = Memory(self.store).history('fake')
        self.assertEqual(len(memory), 2)
        self.assertEqual(memory[0]['sources'][0]['evaluation_id'], 'first')
        html = Pages(self.store).render('/models', {'model': ['fake']})
        self.assertIn('Accumulated observations', html)
        self.assertIn('More details', html)

    def test_unclean_attempt_recovery_appends_error_without_relaunch(self):
        flow = self.flow()
        flow.enqueue('benchmark', [{'task_id': 'example', 'version': self.version}],
                     [{'model': 'sample', 'effort': 'high', 'adapter': 'fake'}])
        job = flow.jobs()[0]
        p = job['payload']
        with patch('mineval.benchmark.seal', side_effect=SystemExit('host loss')):
            with self.assertRaises(SystemExit):
                benchmark.run(self.store, 'example', self.version, run_id=p['run_id'],
                    adapter=FakeAdapter(model='sample', effort='high'), sandbox=SimulatedDocker())
        self.assertEqual(self.store.inspect_run('example', p['run_id'])['status'], 'running')
        with patch('mineval.workflows.benchmark.run', side_effect=AssertionError('never rerun an uncertain identity')):
            flow.work()
        self.assertEqual(flow.jobs()[0]['state'], 'failed')
        recovered = self.store.inspect_run('example', p['run_id'])
        self.assertEqual(recovered['status'], 'error')
        self.assertEqual(recovered['failure_kind'], 'unclean_host_exit')
        with self.assertRaisesRegex(ValueError, 'new benchmark attempt'):
            flow.retry(job['id'])
        flow.update(job['id'], state='queued')
        with patch('mineval.workflows.evaluation.evaluate', side_effect=AssertionError('unusable output must not be evaluated')):
            flow.work()
        self.assertIn('no usable saved output', flow.jobs()[0]['detail'])
        html = Pages(self.store, workflows=flow).render('/jobs', {})
        self.assertIn('Start new benchmark attempt', html)
        flow.retry(job['id'], new_attempt=True)
        replacement = flow.jobs()[0]['payload']
        self.assertNotEqual(replacement['run_id'], p['run_id'])
        self.assertNotEqual(replacement['evaluation_id'], p['evaluation_id'])
        with patch('mineval.benchmark.run_agent', side_effect=self._run_agent), \
             patch('mineval.workflows.DockerSandbox', return_value=EvaluationRunner()):
            flow.work()
        self.assertEqual(flow.jobs()[0]['state'], 'completed')
        self.assertEqual(self.store.inspect_run('example', p['run_id']), recovered)


    def test_review_image_and_failure_are_independent_of_evaluation(self):
        config = read_json(self.config)
        write_json(self.config, config | {'image': 'private-tests:local'})
        for settings, expected in [({'review_image': 'review:local', 'preparation_image': 'prep:local'}, 'review:local'),
                                   ({'preparation_image': 'prep:local'}, 'prep:local'),
                                   ({}, 'local:test')]:
            with self.subTest(settings=settings):
                flow = self.flow()
                flow.settings.update(settings)
                result = flow.enqueue('benchmark', [{'task_id': 'example', 'version': self.version}],
                    [{'model': 'sample', 'effort': 'high', 'adapter': 'codex'}], exclude=False)
                jid = result[0]['id']
                with patch('mineval.workflows.adapter', return_value=FakeAdapter(model='sample', effort='high', name='codex')), \
                     patch('mineval.benchmark.run_agent', side_effect=self._run_agent), \
                     patch('mineval.workflows.DockerSandbox', return_value=EvaluationRunner()), \
                     patch('mineval.model_review.review_evaluation', side_effect=ValueError('secret output')) as review:
                    flow.work()
                job = next(j for j in flow.jobs() if j['id'] == jid)
                self.assertEqual(review.call_args.kwargs['image'], expected)
                self.assertEqual(job['stage'], 'model review')
                self.assertIn('Qualitative review failed', job['detail'])
                self.assertNotIn('secret output', job['detail'])
                assessment = self.store.inspect_evaluation('example', job['payload']['evaluation_id'])
                flow.retry(jid)
                with patch('mineval.workflows.benchmark.run', side_effect=AssertionError('saved run')) , \
                     patch('mineval.workflows.evaluation.evaluate', side_effect=AssertionError('saved evaluation')), \
                     patch('mineval.model_review.review_evaluation'):
                    flow.work()
                self.assertEqual(next(j for j in flow.jobs() if j['id'] == jid)['state'], 'completed')
                self.assertEqual(self.store.inspect_evaluation('example', assessment['evaluation_id']), assessment)

    def test_coverage_separates_multiple_conditions_from_unique_tasks(self):
        self.saved('one'); self.assess('one', 'first')
        changed = read_json(self.config)
        changed['commands'][0]['name'] = 'second-condition'
        path = self.root / 'second-condition.json'; write_json(path, changed)
        evaluation.evaluate(self.store, 'example', 'one', path, evaluation_id='second', sandbox=EvaluationRunner())
        metric = summaries(self.store)['models'][0]['overall']
        self.assertEqual(metric['evaluated'], 2)
        self.assertEqual(metric['available_samples'], 2)
        self.assertEqual(metric['sample_coverage'], 1)
        self.assertEqual(metric['covered_tasks'], 1)
        self.assertEqual(metric['available'], 1)
        self.assertEqual(metric['coverage'], 1)

    def test_cli_submission_worker_and_review_history_share_saved_records(self):
        from contextlib import redirect_stdout
        from mineval.cli import main
        import io
        definition = self.root / 'selection.json'
        write_json(definition, {'kind': 'benchmark', 'selections': [{'task_id': 'example', 'version': self.version}],
            'configurations': [{'model': 'cli-model', 'effort': 'high', 'adapter': 'fake'}]})
        settings = self.root / 'settings.json'; write_json(settings, {'evaluation_config': 'evaluation.json'})
        args = ['--data', str(self.store.root), 'workflow', '--settings', str(settings)]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args + ['submit', str(definition)]), 0)
        self.assertEqual(json.loads(output.getvalue())[0]['state'], 'queued')
        with patch('mineval.benchmark.run_agent', side_effect=self._run_agent), \
             patch('mineval.workflows.DockerSandbox', return_value=EvaluationRunner()), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main(args + ['work']), 0)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--data', str(self.store.root), 'model-review', 'history', 'cli-model']), 0)
        history = json.loads(output.getvalue())
        self.assertEqual(history[0]['sources'][0]['model']['reasoning_effort'], 'high')

    def test_graceful_stop_between_stages_keeps_queued_work_retryable(self):
        flow = self.flow()
        flow.enqueue('benchmark', [{'task_id': 'example', 'version': self.version}],
                     [{'model': 'sample', 'effort': 'high', 'adapter': 'fake'}])
        stopped = threading.Event()
        original = benchmark.run
        def run(*args, **kwargs):
            kwargs['sandbox'] = SimulatedDocker()
            result = original(*args, **kwargs)
            stopped.set()
            return result
        with patch('mineval.workflows.benchmark.run', side_effect=run):
            with self.assertRaises(KeyboardInterrupt):
                flow.work(stop_event=stopped)
        job = flow.jobs()[0]
        self.assertEqual(job['state'], 'interrupted')
        self.assertTrue((self.store.task('example') / 'runs' / (job['payload']['run_id'] + '.json')).exists())
        self.assertFalse((self.store.task('example') / 'evaluations' / (job['payload']['evaluation_id'] + '.json')).exists())

    def test_named_category_mean_and_comparable_intersection_use_exact_conditions(self):
        class SolvingAgent(SimulatedDocker):
            @contextmanager
            def container(inner, spec):
                with super().container(spec) as container:
                    original = container.start.side_effect
                    def start():
                        original()
                        (spec.workspace / 'app.py').write_text('def answer(): return 42\n')
                    container.start.side_effect = start
                    yield container
        versions = {}
        for tid, category in [('logic-one', 'behavior'), ('logic-two', 'behavior'), ('access', 'security')]:
            self.store.register(self.candidate | {'task_id': tid, 'category': category})
            write_json(self.store.draft(tid) / 'control/verify.json', read_json(self.config_path))
            diagnose(self.store, tid, sandbox=FakeRunner([step()]))
            versions[tid] = self.freeze(tid, self.review)['version']
            benchmark.run(self.store, tid, versions[tid], run_id='a',
                adapter=FakeAdapter(model='A', effort='high'),
                sandbox=SolvingAgent() if category == 'behavior' else SimulatedDocker())
            evaluation.evaluate(self.store, tid, 'a', self.config, evaluation_id='ea', sandbox=EvaluationRunner())
        benchmark.run(self.store, 'logic-one', versions['logic-one'], run_id='b',
            adapter=FakeAdapter(model='B', effort='high'), sandbox=SolvingAgent())
        evaluation.evaluate(self.store, 'logic-one', 'b', self.config, evaluation_id='eb', sandbox=EvaluationRunner())
        report = summaries(self.store)
        models = {m['model']['name']: m for m in report['models']}
        self.assertAlmostEqual(models['A']['category_average'], .5)
        self.assertAlmostEqual(models['A']['overall']['pass_rate'], 2/3)
        self.assertEqual(models['A']['categories']['security']['fail'], 1)
        self.assertEqual(models['B']['categories']['behavior']['coverage'], .5)
        self.assertEqual(report['common_samples'], 1)
        self.assertEqual(models['A']['comparable']['pass_rate'], 1)
        changed = read_json(self.config)
        changed['commands'][0]['name'] = 'different-condition'
        path = self.root / 'different.json'; write_json(path, changed)
        # Different evaluation conditions create a separate sample, not an equivalent score.
        evaluation.evaluate(self.store, 'logic-two', 'a', path, evaluation_id='other', sandbox=EvaluationRunner())
        updated = summaries(self.store)
        self.assertEqual(updated['common_samples'], 1)
        updated_models = {m['model']['name']: m for m in updated['models']}
        self.assertEqual(updated_models['A']['overall']['sample_coverage'], 1)
        self.assertEqual(updated_models['B']['overall']['sample_coverage'], .25)
        self.assertEqual(updated_models['A']['overall']['coverage'], .75)

    def test_http_writes_require_origin_csrf_and_idempotency_and_retirement_confirmation(self):
        flow = self.flow()
        with make_server(self.store, workflows=flow) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            self.addCleanup(thread.join, 2); self.addCleanup(server.shutdown)
            prefix = urlsplit(server.observation_url).path.rstrip('/')
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            self.addCleanup(connection.close)
            connection.request('GET', prefix + '/settings')
            response = connection.getresponse(); body = response.read().decode()
            self.assertEqual(response.getheader('Referrer-Policy'), 'same-origin')
            csrf = re.search(r'name="csrf" value="([^"]+)"', body)[1]
            fields = {'csrf': csrf, 'request_id': 'test-removal', 'task': 'example', 'operation': 'retire'}
            headers = {'Content-Type': 'application/x-www-form-urlencoded', 'Origin': 'http://127.0.0.1:' + str(server.server_port)}
            def post(path, data, headers=headers):
                connection.request('POST', prefix + '/actions/' + path, urlencode(data), headers)
                response = connection.getresponse(); return response.status, response.read().decode()
            self.assertEqual(post('selection', fields, headers | {'Origin': 'https://untrusted.example'})[0], 403)
            self.assertEqual(post('selection', fields | {'csrf': 'wrong'})[0], 403)
            code, confirmation = post('selection', fields)
            self.assertEqual(code, 200)
            self.assertIn('Confirm task removal', confirmation)
            self.assertEqual(self.store.inspect_task('example')['state']['status'], 'frozen')
            request_id = re.search(r'name="request_id" value="([^"]+)"', confirmation)[1]
            confirmed = {'csrf': csrf, 'request_id': request_id, 'task': 'example', 'confirm': 'retire', 'reason': 'bad evidence'}
            self.assertEqual(post('retire-confirm', confirmed)[0], 200)
            self.assertEqual(self.store.inspect_task('example')['state']['status'], 'retired')
            self.assertIn('Request already received', post('retire-confirm', confirmed)[1])
            self.assertEqual(self.store.inspect_revision('example', self.version)['version'], self.version)

    def test_dashboard_separates_preparation_models_and_benchmark_review(self):
        flow = self.flow()
        flow.catalog.get = lambda **kw: {'models': [{'model': 'catalog-model', 'name': 'Catalog model', 'efforts': ['low', 'high'], 'default_effort': 'high'}], 'error': None}
        pages = Pages(self.store, workflows=flow)
        overview = pages.render('/')
        self.assertIn('Benchmark overview', overview)
        self.assertIn('Run benchmark', overview)
        pool = pages.render('/tasks')
        self.assertLess(pool.index('Prepare all'), pool.index('<table>'))
        self.assertNotIn('name="configuration"', pool)
        self.assertNotIn('name="csrf"', pool)  # GET selection does not put CSRF in URLs.
        confirmation = pages.render('/prepare', {'all': ['yes']})
        self.assertIn('0 tasks will be prepared; 1 skipped', confirmation)
        self.assertEqual(flow.jobs(), [])
        picker = pages.render('/benchmarks/new', {'task': ['example'], 'step': ['models'], 'version_example': [self.version]})
        self.assertIn('Models being benchmarked', picker)
        self.assertIn('catalog-model', picker)
        self.assertIn('value="' + self.version + '"', picker)
        searched = pages.render('/benchmarks/new', {'task': ['example'], 'step': ['models'], 'model_step': ['yes'], 'model_search': ['catalog'], 'models': ['explicit/model'], 'effort': ['ultra']})
        self.assertIn('rows="3">explicit/model</textarea>', searched)
        self.assertIn('<option value="ultra" selected>', searched)
        self.assertNotIn('name="exclude" value="yes" checked', searched)
        config = json.dumps({'model': 'catalog-model', 'effort': 'high', 'adapter': 'codex'})
        selection = {'task': ['example'], 'step': ['review'], 'configuration': [config], 'version_example': [self.version], 'exclude': ['yes']}
        review = pages.render('/benchmarks/new', selection)
        self.assertIn('1 task/model combinations ready to queue', review)
        self.assertIn('AI-assisted evaluation: disabled', review)
        self.assertEqual(flow.jobs(), [])  # Review does not enqueue or spend inference.
        with patch('mineval.benchmark.run_agent', side_effect=self._run_agent):
            benchmark.run(self.store, 'example', self.version, run_id='prior', adapter=FakeAdapter(model='catalog-model', effort='high'))
        selection['configuration'] = [json.dumps({'model':'catalog-model', 'effort':'high', 'adapter':'fake'})]
        skipped = pages.render('/benchmarks/new', selection)
        self.assertIn('Skip · already attempted', skipped)
        self.assertIn('<button disabled>Run benchmark</button>', skipped)
        del flow.settings['evaluation_config']
        unavailable = pages.render('/benchmarks/new', selection)
        self.assertIn('Configure private evaluation commands', unavailable)
        self.assertIn('<button disabled>Run benchmark</button>', unavailable)

    def test_dashboard_read_only_boundary_and_host_review_configuration(self):
        pages = Pages(self.store)
        for route in ('/', '/tasks', '/benchmarks/new', '/settings', '/prepare'):
            html = pages.render(route)
            self.assertIn('Read-only historical view', html)
            self.assertNotIn('method="post"', html)
        flow = self.flow()
        flow.settings.update(review_model='host-review', review_effort='ultra')
        settings = Pages(self.store, workflows=flow).render('/settings')
        self.assertIn('host-review · ultra', settings)
        self.assertIn('host-controlled review override', settings)
        self.assertNotIn('name="review_model"', settings)



class PreparationWorkflowTests(HistoryFixture):
    def test_full_pipeline_uses_separate_sessions_and_only_freezes_approval(self):
        from mineval import service, review
        self.store.register(self.candidate | {'task_id': 'rejected-task'})
        write_json(self.store.draft('rejected-task') / 'control/verify.json', read_json(self.config_path))
        diagnose(self.store, 'rejected-task', sandbox=FakeRunner([step()]))
        flow = Workflows(self.store, {'preparation_model': 'prep-selected', 'preparation_effort': 'ultra'})
        flow.enqueue('prepare', [{'task_id': 'example'}, {'task_id': 'rejected-task'}])
        flow.settings['preparation_model'] = 'changed-after-queue'
        from mineval.cleanup import save_attempt
        from mineval.artifacts import copy_tree, inventory
        from mineval.sandbox import RunResult
        def cleanup(store, tid, **kwargs):
            self.assertEqual(kwargs['adapter'].model, 'prep-selected')
            self.assertEqual(kwargs['adapter'].effort, 'ultra')
            with store.operation(tid), tempfile.TemporaryDirectory(dir=store.root) as temp:
                temp = Path(temp)
                copy_tree(store.draft(tid) / 'workspace', temp / 'clean')
                control = temp / 'control'; control.mkdir()
                write_json(control / 'verify.json', read_json(store.draft(tid) / 'control/verify.json'))
                write_json(control / 'cleanup-report.json', {
                    'disposition': 'ready-for-review', 'summary': 'Fixture cleanup',
                    'objective_preserved': True, 'changes': [],
                    'environment': {'runtime': 'fixture', 'dependencies': [], 'services': [], 'limitations': []}})
                diagnostic = read_json(store.task(tid) / 'verify-latest.json')
                return save_attempt(store, tid, temp / 'clean', control,
                    RunResult('fixture', 'image', 0, False, False, '', '', False, 0),
                    image_id='image', diagnostic_ids=[diagnostic['diagnostic_id']],
                    producer={'harness': 'fixture'}, input_hashes=inventory(temp / 'clean'), instructions_sha256='fixture')
        calls = []
        original_session = review.session
        def private(store, tid, **kwargs):
            self.assertEqual(kwargs['adapter'].model, 'prep-selected')
            self.assertEqual(kwargs['adapter'].effort, 'ultra')
            calls.append(kwargs['stage'])
            if kwargs['stage'] == 'extraction':
                kwargs['sandbox'] = ReportAgent('selection.json', plan(self.head) | {
                    'evaluation_config': {'image': 'local:test', 'commands': [
                        {'name': 'private', 'kind': 'tests', 'argv': ['python3', 'tests/test_answer.py']}]}})
            else:
                kwargs['sandbox'] = ReportAgent('review.json', report(self.review) |
                    ({'verdict': 'reject', 'reasons': ['Fixture evidence does not establish the task']} if tid == 'rejected-task' else {}))
            return original_session(store, tid, **kwargs)
        with patch('mineval.workflows.service.session', side_effect=cleanup), \
             patch('mineval.workflows.review.session', side_effect=private), \
             patch('mineval.workflows.DockerSandbox', return_value=FakeRunner([step()])):
            flow.work()
        self.assertEqual(calls, ['extraction', 'review', 'extraction', 'review'])
        self.assertEqual({j['task']: j['state'] for j in flow.jobs()},
                         {'example': 'completed', 'rejected-task': 'failed'})
        self.assertFalse(list((self.store.task('rejected-task') / 'versions').glob('*')))
        state = self.store.inspect_task('example')['state']
        self.assertEqual(state['status'], 'frozen')
        frozen = self.store.task('example') / 'versions' / state['version']
        self.assertFalse((frozen / 'tests/test_answer.py').exists())
        self.assertTrue((frozen / 'private/verification/extraction-manifest.json').exists())
        self.assertEqual(flow.eval_config('example', state['version'])['commands'][0]['name'], 'private')
        failed = next(j for j in flow.jobs() if j['task'] == 'rejected-task')
        flow.retry(failed['id'])
        def approved(store, tid, **kwargs):
            self.assertEqual(kwargs['stage'], 'review')
            kwargs['sandbox'] = ReportAgent('review.json', report(self.review))
            return original_session(store, tid, **kwargs)
        with patch('mineval.workflows.service.session', side_effect=AssertionError('cleanup repeated')), \
             patch('mineval.workflows.verification.diagnose', side_effect=AssertionError('diagnostics repeated')), \
             patch('mineval.workflows.review.validate', side_effect=AssertionError('validation repeated')), \
             patch('mineval.workflows.review.session', side_effect=approved), \
             patch('mineval.workflows.DockerSandbox', return_value=FakeRunner([step()])):
            flow.work()
        self.assertEqual({j['state'] for j in flow.jobs()}, {'completed'})


    def test_invalid_new_category_rejected_and_missing_legacy_category_preserved(self):
        with self.assertRaisesRegex(ValueError, 'category'):
            self.store.register(self.candidate | {'task_id': 'invalid', 'category': 'misc'})
        self.store.register(self.candidate | {'task_id': 'tagged', 'category': 'behavior'})
        self.store.register(self.candidate | {'task_id': 'legacy-category', 'category': 'feature'})
        pool = {t['task_id']: t for t in tasks(self.store)}
        self.assertEqual(pool['example']['category'], 'uncategorized')
        self.assertEqual(pool['tagged']['category'], 'behavior')
        self.assertEqual(pool['legacy-category']['category'], 'uncategorized')
        self.assertEqual(self.store.inspect_task('legacy-category')['metadata']['category'], 'feature')


class CLIConcurrencyTests(unittest.TestCase):
    setUp = MemoryTests.setUp
    append = MemoryTests.append
    def test_two_cli_agents_cannot_claim_same_pair_and_commits_survive_process_retry(self):
        import subprocess
        import sys
        for n in range(24):
            self.append(n)
        command = [sys.executable, '-m', 'mineval', '--data', str(self.store.root), 'model-review']
        def claim(_):
            return json.loads(subprocess.check_output(command + ['claim', 'model/a']))
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, range(2)))
        claimed, = [c for c in claims if c]
        body = Path(self.temp.name) / 'summary.json'; write_json(body, ['Preserve original grounded findings'])
        def commit(_):
            return json.loads(subprocess.check_output(command + ['commit', claimed['reservation_id'], str(body)]))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(commit, range(2)))
        self.assertEqual(results[0]['id'], results[1]['id'])
        self.assertEqual(len(Memory(self.store).history('model/a')), 23)


class ModelReviewTests(HistoryFixture):
    def setUp(self):
        super().setUp()
        self.version = self.freeze('example', self.review)['version']
        self.config = self.root / 'evaluation.json'
        write_json(self.config, {'image': 'local:test', 'commands': [
            {'name': 'private', 'kind': 'tests', 'argv': ['sh', '-c', 'PYTHONPATH=. python3 tests/test_answer.py']}]})
    saved = fixtures.EvaluationTests.saved
    assess = fixtures.EvaluationTests.assess
    def test_review_receives_prior_evidence_and_code_without_changing_verdict(self):
        from mineval.model_review import review_evaluation
        self.saved('one'); self.assess('one', 'first')
        self.saved('two', 'def answer(): return 7\n'); self.assess('two', 'second')
        captured = {}
        class EvidenceAgent(ReportAgent):
            @contextmanager
            def container(inner, spec):
                captured.update(read_json(spec.workspace / 'input.json'))
                with super().container(spec) as container:
                    yield container
        agent = EvidenceAgent('observations.json', ['example/second: saved implementation returned 7; the private answer regression failed.'])
        with patch('mineval.model_review.DockerSandbox', return_value=agent):
            result = review_evaluation(self.store, 'example', 'second', image='local:test')
        self.assertIn('return 7', captured['code_diff'])
        self.assertIn('assert answer() == 42', captured['selected_verification'][0]['bounded_content'])
        self.assertTrue(captured['prior'])
        self.assertEqual(captured['source']['evaluation_id'], 'second')
        self.assertEqual(result['sources'][0]['verdict'], 'fail')
        self.assertEqual(self.store.inspect_evaluation('example', 'second')['verdict'], 'fail')
        self.assertEqual(len(Memory(self.store).history('fake')), 3)
        with patch('mineval.model_review.infer', side_effect=AssertionError('duplicate review must not spend another inference')):
            duplicate = review_evaluation(self.store, 'example', 'second', image='local:test')
        self.assertEqual(result['id'], duplicate['id'])


def setUpModule():
    global resource_patch
    resource_patch = patch('mineval.resources.capacity', return_value=(64, 131072))
    resource_patch.start()


def tearDownModule():
    resource_patch.stop()
