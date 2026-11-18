"""Incremental execution: real Git/artifacts, bounded local fixture workloads."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
import threading
import time
from unittest.mock import patch

from mineval import benchmark
from mineval.agents import FakeAdapter, run_agent
from mineval.artifacts import digest, inventory, read_json, write_json
from mineval.checkpoints import PreparationCheckpoints
from mineval.performance import summaries
from mineval.workflows import Workflows
from test_agents import SimulatedDocker
from test_evaluation import EvaluationRunner
from test_preparation import HistoryFixture


class IncrementalTests(HistoryFixture):
    def setUp(self):
        super().setUp()
        self.fixture_generated_cases = True
        self.fixture_evaluation_config = {'image': 'local:test', 'network': 'none', 'grading_type': 'isolated-argv-v1',
            'commands': [{'name': 'answer-regression', 'kind': 'tests',
                          'argv': ['python3', '-c', 'from app import answer; print(answer())']}]}
        self.version = self.freeze('example', self.review)['version']
        self.eval_config = self.root / 'evaluation.json'
        write_json(self.eval_config, self.fixture_evaluation_config)

    def flow(self, **settings):
        return Workflows(self.store, {'evaluation_config': str(self.eval_config),
            'evaluator_agent': False, 'concurrency': {'total': 4},
            'resources': {'admission_interval_seconds': .01}, **settings})

    def enqueue(self, flow, *models):
        return flow.enqueue('benchmark', [{'task_id': 'example', 'version': self.version}],
            [{'model': name, 'adapter': 'fake'} for name in models])

    def agent(self, *args, **kwargs):
        kwargs['sandbox'] = SimulatedDocker()
        return run_agent(*args, **kwargs)

    def test_same_task_runs_overlap_and_recovery_cannot_steal_active_identity(self):
        rendezvous = threading.Barrier(2)
        def agent(*args, **kwargs):
            rendezvous.wait(timeout=5)
            with self.assertRaisesRegex(ValueError, 'busy'):
                benchmark.recover(self.store, 'example', kwargs['adapter'].model)
            return self.agent(*args, **kwargs)
        with patch('mineval.benchmark.run_agent', side_effect=agent):
            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(benchmark.run, self.store, 'example', self.version,
                    run_id=model, adapter=FakeAdapter(model=model), sandbox=SimulatedDocker()) for model in ('a', 'b')]
                results = [f.result(timeout=10) for f in futures]
        self.assertEqual([r['status'] for r in results], ['completed', 'completed'])
        self.assertNotEqual(results[0]['run_id'], results[1]['run_id'])

    def test_materialized_history_is_reconstructed_once_and_clones_are_isolated(self):
        from mineval.history import restore
        from mineval.artifacts import unpack
        with patch('mineval.history.restore', wraps=restore) as restored, \
             patch('mineval.preparation.unpack', wraps=unpack) as extracted:
            for name in ('first', 'second'):
                self.store.materialize('example', self.version, self.root / name)
        self.assertEqual(restored.call_count, 1)
        self.assertEqual(extracted.call_count, 1)
        first, second = self.root / 'first/app.py', self.root / 'second/app.py'
        self.assertNotEqual(first.stat().st_ino, second.stat().st_ino)
        first.write_text('changed')
        self.assertEqual(second.read_text(), 'def answer(): return 0\n')
        template = next((self.store.root / 'materialized').iterdir())
        (template / 'tree/app.py').write_text('corrupt')
        with self.assertRaisesRegex(ValueError, 'starting state hash mismatch'):
            self.store.materialize('example', self.version, self.root / 'third')

    def test_review_does_not_hold_benchmark_slots_and_blocks_publication(self):
        flow = self.flow()
        self.enqueue(flow, 'a')
        reviewing, other_benchmarked = threading.Event(), threading.Event()
        original = flow.benchmark
        def phases(jid, task, payload, *, phase=None):
            if phase == 'review' and payload['configuration']['model'] == 'a':
                reviewing.set()
                self.assertTrue(summaries(self.store)['models'])
                # Enqueue while review is running, as an independent HTTP request would.
                self.enqueue(flow, 'b')
                self.assertTrue(other_benchmarked.wait(5), 'review blocked independent benchmark')
            result = original(jid, task, payload, phase=phase)
            if phase == 'benchmark' and payload['configuration']['model'] == 'b':
                other_benchmarked.set()
            return result
        with patch.object(flow, 'benchmark', side_effect=phases), \
             patch('mineval.benchmark.run_agent', side_effect=self.agent), \
             patch('mineval.workflows.DockerSandbox', return_value=EvaluationRunner()):
            flow.work()
        self.assertTrue(reviewing.is_set())
        self.assertEqual({j['state'] for j in flow.jobs()}, {'completed'})
        self.assertEqual(len(summaries(self.store)['models']), 2)

    def test_bounded_execution_and_phase_recovery(self):
        flow = self.flow(concurrency={'benchmark': 2, 'total': 3})
        self.enqueue(flow, 'a', 'b', 'c', 'd')
        count, peak = 0, 0
        guard = threading.Lock()
        def slow_agent(*args, **kwargs):
            nonlocal count, peak
            with guard:
                count += 1; peak = max(count, peak)
            try:
                time.sleep(.1)
                return self.agent(*args, **kwargs)
            finally:
                with guard:
                    count -= 1
        with patch('mineval.benchmark.run_agent', side_effect=slow_agent), \
             patch('mineval.workflows.DockerSandbox', return_value=EvaluationRunner()):
            flow.work()
        self.assertEqual(peak, 2)
        self.assertEqual({j['state'] for j in flow.jobs()}, {'completed'})

    def test_interrupted_evaluation_retry_does_not_publish_abandoned_assessment(self):
        from mineval import evaluation
        from mineval.publication import published
        flow = self.flow()
        self.enqueue(flow, 'retry-evaluation')
        with patch('mineval.benchmark.run_agent', side_effect=self.agent), \
             patch('mineval.evaluation.execute_checks', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                flow.work()
        failed = flow.jobs()[0]
        old_id = failed['payload']['evaluation_id']
        self.assertEqual(self.store.inspect_evaluation('example', old_id)['error_kind'], 'interrupted')
        flow.retry(failed['id'])
        with patch('mineval.workflows.benchmark.run', side_effect=AssertionError('benchmark repeated')), \
             patch('mineval.workflows.DockerSandbox', return_value=EvaluationRunner()):
            flow.work()
        finished = flow.jobs()[0]
        self.assertEqual(finished['state'], 'completed')
        self.assertNotEqual(finished['payload']['evaluation_id'], old_id)
        self.assertFalse(published(self.store, 'example', finished['payload']['run_id'], old_id))
        self.assertTrue(published(self.store, 'example', finished['payload']['run_id'], finished['payload']['evaluation_id']))
        self.assertEqual(summaries(self.store)['models'][0]['overall']['fail'], 1)
        ref = {'task_id': 'example', 'evaluation_id': old_id}
        with self.assertRaisesRegex(ValueError, 'not complete'):
            evaluation.compare(self.store, {'left': [ref], 'right': [ref]})

    def test_starting_state_prune_keeps_private_clones_and_frozen_receipts(self):
        self.store.materialize('example', self.version, self.root / 'private')
        frozen = self.store.inspect_revision('example', self.version)
        self.assertEqual(self.store.starting_cache()['states'], 1)
        self.assertEqual(self.store.starting_cache(max_bytes=0)['states'], 0)
        self.assertTrue((self.root / 'private/.git').exists())
        self.assertEqual(self.store.inspect_revision('example', self.version), frozen)
        self.store.materialize('example', self.version, self.root / 'rebuilt')
        self.assertEqual(self.store.starting_cache()['states'], 1)

    def test_concurrent_evaluations_ignore_unsealed_other_runs_and_own_ids(self):
        from mineval import evaluation
        for model in ('one', 'two'):
            benchmark.run(self.store, 'example', self.version, run_id=model,
                          adapter=FakeAdapter(model=model), sandbox=SimulatedDocker())
        record_written, release = threading.Event(), threading.Event()
        def publish(path, value):
            write_json(path, value)
            if path.name == 'first.json' and path.parent.name == 'evaluations':
                record_written.set()
                self.assertTrue(release.wait(10))
        def assess(run, eid):
            return evaluation.evaluate(self.store, 'example', run, self.eval_config,
                                       evaluation_id=eid, sandbox=EvaluationRunner())
        with patch('mineval.preparation.write_json', side_effect=publish):
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(assess, 'one', 'first')
                try:
                    self.assertTrue(record_written.wait(5))
                    with self.assertRaisesRegex(ValueError, 'busy'):
                        assess('two', 'first')
                    second = pool.submit(assess, 'two', 'second').result(timeout=5)
                    self.assertEqual(second['evaluation_version'], 1)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=5)['evaluation_version'], 1)

    def test_hash_cache_invalidates_on_replacement_and_restored_mtime(self):
        path = self.root / 'hash-me'; path.write_text('old')
        before, info = digest(path), path.stat()
        path.write_text('new')
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertNotEqual(digest(path), before)
        path.unlink(); path.write_text('end')
        self.assertNotEqual(digest(path), before)

    def test_hash_rechecks_bytes_when_metadata_identity_is_unchanged(self):
        path = self.root / 'coarse-clock'; path.write_text('old')
        with patch('mineval.artifacts._identity', return_value=('unchanged',)):
            before = digest(path)
            path.write_text('new')
            self.assertNotEqual(digest(path), before)

    def test_preparation_checkpoints_revalidate_and_invalidate_downstream(self):
        progress = PreparationCheckpoints(self.store, 'example', lambda image: image)
        cleanup = read_json(self.store.task('example') / 'cleanup-latest.json')
        calls = []
        def stage():
            calls.append('cleanup')
            return cleanup
        key = progress.state()
        progress.run('cleanup', key, stage, progress.cleanup)
        restarted = PreparationCheckpoints(self.store, 'example', lambda image: image)
        restarted.run('cleanup', key, stage, progress.cleanup)
        self.assertEqual(calls, ['cleanup'])
        (self.workspace / 'additional.txt').write_text('changed input')
        # The key changes and the old cleanup no longer verifies as current.
        self.assertNotEqual(progress.state(), key)
        self.assertFalse(progress.cleanup(cleanup))
        restarted.run('cleanup', progress.state(), stage, progress.cleanup)
        self.assertEqual(calls, ['cleanup', 'cleanup'])
        # A changed metadata timestamp cannot mask tampering with sealed evidence.
        attempt = self.store.task('example') / 'cleanups' / cleanup['cleanup_id']
        (attempt / 'tree-hashes.json').chmod(0o644)
        (attempt / 'tree-hashes.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            progress.cleanup(cleanup)

    def test_large_hard_limit_does_not_reserve_maximum_capacity(self):
        from mineval.publication import published
        flow = self.flow()
        self.fixture_evaluation_config = self.fixture_evaluation_config | {'limits': {'memory_mb': 8192}}
        large_version = self.freeze('example', self.review)['version']
        flow.enqueue('benchmark', [{'task_id': 'example', 'version': large_version}],
                     [{'model': 'large', 'adapter': 'fake'}])
        self.enqueue(flow, 'small')
        with patch('mineval.resources.capacity', return_value=(8, 4096)), patch('mineval.benchmark.run_agent', side_effect=self.agent), patch('mineval.workflows.DockerSandbox', return_value=EvaluationRunner()):
            flow.work()
        jobs = {j['payload']['configuration']['model']: j for j in flow.jobs()}
        self.assertEqual(jobs['small']['state'], 'completed')
        self.assertEqual(jobs['large']['state'], 'completed')
        self.assertTrue(published(self.store, 'example', jobs['large']['payload']['run_id']))

    def test_partial_evaluation_receipt_recovery(self):
        from mineval import evaluation
        from mineval.publication import published
        for boundary in ('seal', 'receipt'):
            with self.subTest(boundary=boundary):
                flow = self.flow()
                self.enqueue(flow, 'partial-' + boundary)
                original = self.store._reserve_record
                seal = evaluation.seal
                def partial(task, namespace, identity, value):
                    if namespace == 'evaluations':
                        write_json(self.store.task(task) / namespace / (identity + '.json'), value)
                        raise KeyboardInterrupt()
                    return original(task, namespace, identity, value)
                def after_seal(staged, target):
                    seal(staged, target)
                    raise KeyboardInterrupt()
                target = patch.object(self.store, '_reserve_record', side_effect=partial) if boundary == 'receipt' else patch('mineval.evaluation.seal', side_effect=after_seal)
                with patch('mineval.benchmark.run_agent', side_effect=self.agent), patch('mineval.workflows.DockerSandbox', return_value=EvaluationRunner()), target:
                    with self.assertRaises(KeyboardInterrupt):
                        flow.work()
                job = next(j for j in flow.jobs() if j['state'] == 'interrupted')
                old = job['payload']['evaluation_id']
                self.assertFalse(published(self.store, 'example', job['payload']['run_id'], old))
                flow.retry(job['id'])
                with patch('mineval.workflows.benchmark.run', side_effect=AssertionError('repeated run')), patch('mineval.workflows.DockerSandbox', return_value=EvaluationRunner()):
                    flow.work()
                finished = next(j for j in flow.jobs() if j['id'] == job['id'])
                self.assertEqual(finished['state'], 'completed', finished)
                self.assertNotEqual(old, finished['payload']['evaluation_id'])
                self.assertFalse(published(self.store, 'example', finished['payload']['run_id'], old))


def setUpModule():
    global resource_patch, pressure_patch
    resource_patch = patch('mineval.resources.capacity', return_value=(64, 131072))
    resource_patch.start()
    pressure_patch = patch('mineval.resources.pressure', return_value={})
    pressure_patch.start()


def tearDownModule():
    resource_patch.stop()
    pressure_patch.stop()
