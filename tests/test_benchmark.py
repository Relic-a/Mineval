"""Phase 6 acceptance: local Git, fake agent, durable results, no paid calls."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from mineval import benchmark
from mineval.agents import FakeAdapter
from mineval.artifacts import digest, inventory, read_json, write_json
from mineval.cli import main
from mineval.preparation import Store
from mineval.sandbox import DockerSandbox, SandboxError
from test_agents import SimulatedDocker
from test_preparation import HistoryFixture


class BenchmarkTests(HistoryFixture):
    def frozen(self):
        return self.freeze('example', self.review)['version']

    def run_fake(self, version, run_id='attempt-1', **options):
        return benchmark.run(self.store, 'example', version, run_id=run_id,
                             sandbox=options.pop('sandbox', SimulatedDocker()), **options)

    def test_two_attempts_restart_exports_private_boundary_and_immutable_identity(self):
        version = self.frozen()
        frozen = self.store.task('example') / 'versions' / version
        before = inventory(frozen)
        # Both the private directory names and bytes should never reach an agent.
        class InspectedDocker(SimulatedDocker):
            @contextmanager
            def container(inner, spec):
                paths = inventory(spec.workspace, allow_git=True)
                self.assertFalse(any(p.startswith(('private/', 'future/', 'tests/')) for p in paths))
                self.assertNotIn('tests/test_answer.py', paths)
                self.assertEqual((spec.workspace / 'app.py').read_text(), 'def answer(): return 0\n')
                prompt = (spec.inputs / 'prompt.txt').read_text()
                self.assertNotIn(self.head, prompt)
                self.assertNotIn(self.candidate['repository'], prompt)
                self.assertIn('Ordinary outbound web access is allowed', prompt)
                self.assertIn('self-reported', prompt)
                self.assertIsNone(spec.broker)
                self.assertFalse(spec.retain_logs)
                self.assertEqual(spec.network, 'bridge')
                with super().container(spec) as container:
                    yield container
        first = self.run_fake(version, sandbox=InspectedDocker())
        second = self.run_fake(version, 'attempt-2', sandbox=InspectedDocker())
        self.assertEqual(first['status'], 'completed')
        self.assertIsNone(first['task_verdict'])
        self.assertEqual(first['agent_result']['instructions_sha256'], first['instructions_sha256'])
        self.assertEqual(first['agent_result']['completion']['historical_solution_seen'], 'no')
        reopened = Store(self.store.root)
        self.assertEqual(reopened.inspect_run('example', 'attempt-1'), first)
        self.assertEqual(len(benchmark.list_runs(reopened)), 2)
        for record in (first, second):
            output = self.root / record['run_id']
            benchmark.export(reopened, 'example', record['run_id'], output)
            self.assertTrue((output / 'mineval-fake-result.txt').exists())
            self.assertFalse((output / '.git').exists())
            self.assertFalse((output / 'tests/test_answer.py').exists())
        self.assertEqual(inventory(frozen), before)
        process = subprocess.run([sys.executable, '-m', 'mineval', '--data', str(self.store.root),
                                  'bench', 'show', 'example', 'attempt-1'], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), first)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.run_fake(version)
        with self.assertRaisesRegex(ValueError, 'outside the store'):
            benchmark.export(self.store, 'example', 'attempt-1', self.store.root / 'export')
        path = benchmark.output_path(self.store, 'example', 'attempt-1')
        self.assertEqual(set(p.name for p in path.iterdir()), {'agent', 'terminal.json', 'artifact-hashes.json'})
        self.assertEqual(set(p.name for p in (path / 'agent').iterdir()), {'workspace.tar.gz', 'result.json'})
        archive = path / 'agent/workspace.tar.gz'
        archive.chmod(0o600); archive.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            reopened.inspect_run('example', 'attempt-1')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            benchmark.export(reopened, 'example', 'attempt-1', self.root / 'bad')
        self.assertFalse((self.root / 'bad').exists())

    def test_resource_failures_are_not_model_crashes(self):
        version = self.frozen()
        for oom, missing_completion in ((False, False), (True, False), (False, True)):
            class ResourceFailure(SimulatedDocker):
                @contextmanager
                def container(inner, spec):
                    with super().container(spec) as container:
                        original = container.inspect.side_effect
                        container.inspect.side_effect = lambda: original() | {'OOMKilled': oom}
                        container.resources = {'contended': not oom, 'oom_events': int(oom)}
                        if missing_completion:
                            original_start = container.start.side_effect
                            def start():
                                original_start()
                                (spec.control / 'final.json').unlink()
                            container.start.side_effect = start
                        yield container
            record = self.run_fake(version, 'resource-' + str(oom) + str(missing_completion),
                                   adapter=FakeAdapter('success' if missing_completion else 'error'), sandbox=ResourceFailure())
            self.assertEqual(record['status'], 'error' if oom or missing_completion else 'crashed')
            self.assertEqual(record['failure_kind'], 'resource_exhaustion' if oom else 'invalid_completion' if missing_completion else 'agent_process')
            self.assertTrue(record['output_available'])
            self.assertIsNone(record['task_verdict'])

    def test_timeout_crash_cancel_and_invalid_output_are_run_failures(self):
        version = self.frozen()
        for outcome, status in [('error', 'crashed'), ('timeout', 'timeout')]:
            record = self.run_fake(version, outcome, adapter=FakeAdapter(outcome))
            self.assertEqual(record['status'], status)
            self.assertTrue(record['output_available'])
            self.assertIsNone(record['task_verdict'])
        cancelled = self.run_fake(version, 'cancel', sandbox=SimulatedDocker(interrupt=True))
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertTrue(cancelled['output_available'])
        self.assertEqual(cancelled['agent_result']['completion'], None)
        record = self.run_fake(version, 'unsafe', sandbox=SimulatedDocker(bad_output=True))
        self.assertEqual(record['status'], 'error')
        self.assertFalse(record['output_available'])
        with self.assertRaisesRegex(ValueError, 'no saved workspace'):
            benchmark.export(self.store, 'example', 'unsafe', self.root / 'unsafe')
        class BrokenDocker:
            @contextmanager
            def container(self, spec):
                raise SandboxError('SENSITIVE arbitrary subprocess output')
                yield
        failed = self.run_fake(version, 'broken', sandbox=BrokenDocker())
        self.assertEqual(failed['status'], 'error')
        self.assertNotIn('SENSITIVE', json.dumps(failed))
        self.assertIsNone(failed['task_verdict'])
        self.assertEqual(Store(self.store.root).inspect_run('example', 'broken'), failed)
        class MalformedCompletion(SimulatedDocker):
            @contextmanager
            def container(inner, spec):
                with super().container(spec) as container:
                    start = container.start.side_effect
                    def malformed():
                        start()
                        (spec.control / 'final.json').write_text('{}')
                    container.start.side_effect = malformed
                    yield container
        invalid = self.run_fake(version, 'invalid-report', sandbox=MalformedCompletion())
        self.assertEqual(invalid['status'], 'error')
        self.assertEqual(invalid['failure_kind'], 'invalid_completion')
        self.assertTrue(invalid['output_available'])

    def test_unclean_exit_can_be_sealed_once_without_rerun(self):
        version = self.frozen()
        # Simulates process death between reservation and agent launch/publication.
        with patch('mineval.benchmark.run_agent', side_effect=SystemExit(99)):
            with self.assertRaises(SystemExit):
                self.run_fake(version, 'abandoned')
        self.assertEqual(self.store.inspect_run('example', 'abandoned')['status'], 'running')
        result = benchmark.recover(Store(self.store.root), 'example', 'abandoned')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['failure_kind'], 'unclean_host_exit')
        self.assertFalse(result['output_available'])
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            benchmark.recover(self.store, 'example', 'abandoned')

    def test_suite_pool_growth_and_common_exact_revision_coverage(self):
        version = self.frozen()
        self.store.create_suite({'suite_id': 'fixed', 'members': [{'task_id': 'example', 'version': version}]})
        suite_before = inventory(self.store.root / 'suites/fixed')
        self.store.register(self.candidate | {'task_id': 'another'})
        result = benchmark.run_suite(self.store, 'fixed', run_id='suite-attempt', sandbox=SimulatedDocker())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['run_id'], 'suite-attempt-001')
        self.assertEqual(result[0]['suite']['suite_id'], 'fixed')
        self.assertEqual(inventory(self.store.root / 'suites/fixed'), suite_before)
        self.run_fake(version, 'right')
        from mineval.verification import diagnose
        from test_preparation import FakeRunner, step
        write_json(self.store.draft('another') / 'control/verify.json', read_json(self.config_path))
        diagnose(self.store, 'another', sandbox=FakeRunner([step()]))
        another_version = self.freeze('another', self.review)['version']
        benchmark.run(self.store, 'another', another_version, run_id='another-run', sandbox=SimulatedDocker())
        second_version = self.frozen()
        self.run_fake(second_version, 'other-revision')
        ref = lambda run_id: {'task_id': 'example', 'run_id': run_id}
        common = benchmark.compare(self.store, {'left': [ref('suite-attempt-001')], 'right': [ref('right')]})
        self.assertEqual(common['coverage']['common'], 1)
        partial = benchmark.compare(self.store, {
            'left': [ref('right'), {'task_id': 'another', 'run_id': 'another-run'}],
            'right': [ref('suite-attempt-001')]})
        self.assertEqual(partial['coverage'], {'left_total': 2, 'right_total': 1,
                                             'common': 1, 'left_fraction': 0.5, 'right_fraction': 1.0})
        different = benchmark.compare(self.store, {'left': [ref('right')], 'right': [ref('other-revision')]})
        self.assertEqual(different['coverage']['common'], 0)
        self.assertEqual(len(different['left_only']), 1)
        self.assertFalse(different['task_verdicts_available'])
        with self.assertRaisesRegex(ValueError, 'one attempt'):
            benchmark.compare(self.store, {'left': [ref('right'), ref('other-revision')], 'right': [ref('right')]})
        with self.assertRaisesRegex(ValueError, 'not a member'):
            self.run_fake(second_version, 'bad-suite', suite='fixed')
        self.assertFalse((self.store.task('example') / 'runs/bad-suite.json').exists())
        self.store.create_suite({'suite_id': 'two', 'members': [
            {'task_id': 'example', 'version': version},
            {'task_id': 'another', 'version': another_version}]})
        cancelled = benchmark.run_suite(self.store, 'two', run_id='cancelled-suite',
                                        sandbox=SimulatedDocker(interrupt=True))
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]['status'], 'cancelled')
        self.assertFalse((self.store.task('another') / 'runs/cancelled-suite-002.json').exists())

    def test_cli_run_list_compare_export_and_exit_codes(self):
        import contextlib
        import io
        version = self.frozen()
        definition = self.root / 'comparison.json'
        ref = {'task_id': 'example', 'run_id': 'cli'}
        write_json(definition, {'left': [ref], 'right': [ref]})
        with patch('mineval.agents.DockerSandbox', return_value=SimulatedDocker()):
            for args in (['run', '--task', 'example', '--version', version, '--run-id', 'cli'],
                         ['list', '--task', 'example'], ['show', 'example', 'cli'],
                         ['compare', str(definition)], ['export', 'example', 'cli', str(self.root / 'export')]):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = main(['--data', str(self.store.root), 'bench', *args])
                self.assertEqual(code, 0, out.getvalue())
                self.assertTrue(json.loads(out.getvalue()))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(['--data', str(self.store.root), 'bench', 'run', '--task', 'example'])
        self.assertEqual(code, 125)
        self.assertIn('--version', err.getvalue())
        with patch('mineval.benchmark.run', return_value={'status': 'timeout'}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--data', str(self.store.root), 'bench', 'run',
                                      '--task', 'example', '--version', version]), 124)

    def test_summary_is_frozen_and_invalid_revision_does_not_launch(self):
        version = self.frozen()
        metadata = self.store.task('example') / 'metadata.json'
        write_json(metadata, read_json(metadata) | {'task_summary': 'later summary'})
        record = self.run_fake(version)
        self.assertEqual(record['task_instructions'], 'Return the correct answer')
        with self.assertRaises(ValueError):
            self.run_fake('latest', 'invalid')
        self.assertFalse((self.store.task('example') / 'runs/invalid.json').exists())


@unittest.skipUnless(os.environ.get('MINEVAL_AGENT_TEST_IMAGE'), 'set MINEVAL_AGENT_TEST_IMAGE for live rootless fake benchmark')
class LiveBenchmarkTests(HistoryFixture):
    def test_live_independent_attempts_and_pinned_suite(self):
        cfg = read_json(self.config_path) | {'image': os.environ['MINEVAL_AGENT_TEST_IMAGE']}
        write_json(self.config_path, cfg)
        from mineval.verification import diagnose
        runner = DockerSandbox(context=os.environ.get('MINEVAL_TEST_CONTEXT'))
        diagnose(self.store, 'example', sandbox=runner)
        version = self.freeze('example', self.review)['version']
        before = inventory(self.store.task('example') / 'versions' / version)
        self.store.create_suite({'suite_id': 'live', 'members': [{'task_id': 'example', 'version': version}]})
        for index in range(2):
            records = benchmark.run_suite(self.store, 'live', run_id=f'live-{index}', sandbox=runner)
            self.assertEqual(records[0]['status'], 'completed', records)
            output = self.root / f'export-{index}'
            benchmark.export(Store(self.store.root), 'example', records[0]['run_id'], output)
            self.assertTrue((output / 'mineval-fake-result.txt').exists())
            self.assertFalse((output / 'tests/test_answer.py').exists())
        self.assertEqual(inventory(self.store.task('example') / 'versions' / version), before)

    def test_live_cancellation_stops_writer_and_preserves_partial_tree(self):
        from mineval.verification import diagnose
        write_json(self.config_path, read_json(self.config_path) | {'image': os.environ['MINEVAL_AGENT_TEST_IMAGE']})
        runner = DockerSandbox(context=os.environ.get('MINEVAL_TEST_CONTEXT'))
        diagnose(self.store, 'example', sandbox=runner)
        version = self.freeze('example', self.review)['version']
        class InterruptingDocker(DockerSandbox):
            @contextmanager
            def container(inner, spec):
                with super().container(spec) as container:
                    def interrupt(timeout):
                        result = container.exec(['sh', '-c', 'echo partial > /workspace/partial.txt'], timeout=10)
                        self.assertEqual(result['exit_code'], 0)
                        raise KeyboardInterrupt()
                    container.wait = interrupt
                    yield container
        record = benchmark.run(self.store, 'example', version, adapter=FakeAdapter('timeout'),
                               sandbox=InterruptingDocker(context=os.environ.get('MINEVAL_TEST_CONTEXT')))
        self.assertEqual(record['status'], 'cancelled')
        self.assertTrue(record['output_available'])
        benchmark.export(self.store, 'example', record['run_id'], self.root / 'partial')
        self.assertEqual((self.root / 'partial/partial.txt').read_text(), 'partial\n')
