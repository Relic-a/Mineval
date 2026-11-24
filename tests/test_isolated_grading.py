"""Isolated argv grading, including an opt-in real rootless Docker path."""
from contextlib import contextmanager
import os
from pathlib import Path
import unittest
from unittest.mock import Mock

from mineval.historical import benchmark, evaluation
from mineval.core.artifacts import write_json, read_json
from mineval.preparation import Store
from mineval.core.sandbox import DockerSandbox
from mineval.historical.verification import diagnose
from test_agents import SimulatedDocker
from test_evaluation import EvaluationRunner
from test_preparation import HistoryFixture, FakeRunner, step


class IsolatedGradingTests(HistoryFixture):
    def setUp(self):
        super().setUp()
        write_json(self.repo / 'tests/test_answer.py', {'cases': [
            {'input': '7', 'expected': '14\n'}, {'input': '8', 'expected': '16\n'}]})
        self.git('add', '.'); self.git('commit', '-qm', 'synthetic isolated cases')
        self.head = self.git('rev-parse', 'HEAD')
        self.candidate['head_commit'] = self.head
        self.store = Store(self.root / 'isolated-data')
        self.store.register(self.candidate)
        self.workspace = self.store.draft('example') / 'workspace'
        self.config_path = self.store.draft('example') / 'control/verify.json'
        write_json(self.config_path, {'image': 'local:test', 'network': 'bridge', 'commands': [
            {'name': 'smoke', 'kind': 'smoke', 'argv': ['true']}]})
        diagnose(self.store, 'example', sandbox=FakeRunner([step()]))
        self.fixture_evaluation_config = {'image': os.environ.get('MINEVAL_TEST_IMAGE', 'local:test'),
            'network': 'bridge', 'grading_type': 'isolated-argv-v1', 'commands': [
                {'name': 'cases', 'kind': 'tests', 'argv': ['python3', 'app.py']}]}
        self.version = self.freeze('example', self.review)['version']
        self.config = self.root / 'isolated-config.json'
        write_json(self.config, self.fixture_evaluation_config)

    def save(self, code):
        class Candidate(SimulatedDocker):
            @contextmanager
            def container(inner, spec):
                with super().container(spec) as container:
                    start = container.start.side_effect
                    def save_candidate():
                        start()
                        (spec.workspace / 'app.py').write_text(code)
                    container.start.side_effect = save_candidate
                    yield container
        benchmark.run(self.store, 'example', self.version, run_id='candidate', sandbox=Candidate())

    def test_unsupported_legacy_fails_closed(self):
        self.save('print(1)')
        config = read_json(self.config); config.pop('grading_type'); write_json(self.config, config)
        runner = Mock()
        with self.assertRaisesRegex(ValueError, 'differs from the frozen'):
            evaluation.evaluate(self.store, 'example', 'candidate', self.config, sandbox=runner)
        runner.container.assert_not_called()
        unsupported = evaluation.execute_checks(self.workspace, self.root / 'unused-workspace',
            self.root / 'legacy-evidence', self.root, [], [], config, config['image'], runner)
        self.assertEqual(unsupported['error_kind'], 'unsupported_grading_type')
        runner.container.assert_not_called()

    def test_wrong_output_fails_without_shared_evaluator(self):
        self.save('print(0)')
        runner = EvaluationRunner()
        result = evaluation.evaluate(self.store, 'example', 'candidate', self.config, sandbox=runner)
        self.assertEqual(result['verdict'], 'fail', result)
        self.assertEqual(len(result['commands']), 2)
        self.assertEqual(len(runner.specs), 1)
        self.assertIsNone(runner.specs[0].inputs)
        self.assertEqual(runner.specs[0].network, 'bridge')

    @unittest.skipUnless(os.environ.get('MINEVAL_TEST_IMAGE'), 'requires preloaded rootless Docker test image')
    def test_real_grading_boundary_and_network(self):
        # This program probes both the original destination and the grader host path,
        # then makes an actual outbound request before answering the private cases.
        private = self.store.task('example') / 'versions' / self.version / 'private/verification'
        code = '''import pathlib, sys, urllib.request\nassert not pathlib.Path('/workspace/tests/test_answer.py').exists()\nassert not pathlib.Path(%r).exists()\nassert urllib.request.urlopen('https://example.com', timeout=10).status == 200\nprint(int(sys.argv[1]) * 2)\n''' % str(private)
        self.save(code)
        runner = DockerSandbox(context=os.environ.get('MINEVAL_TEST_CONTEXT'))
        result = evaluation.evaluate(self.store, 'example', 'candidate', self.config, sandbox=runner)
        self.assertEqual(result['verdict'], 'pass', result)
        self.assertEqual(len(result['commands']), 2)

    @unittest.skipUnless(os.environ.get('MINEVAL_TEST_IMAGE'), 'requires preloaded rootless Docker test image')
    def test_private_test_fixture_and_expected_files_are_not_mounted(self):
        private = self.root / 'private-probe'
        private.mkdir()
        write_json(private / 'cases.json', {'cases': [{'input': 'hello', 'expected': 'hello\n'}]})
        (private / 'fixture.txt').write_text('synthetic private fixture')
        (private / 'expected.txt').write_text('synthetic expected result')
        source = self.root / 'probe-candidate'; source.mkdir()
        paths = [str(private / name) for name in ('cases.json', 'fixture.txt', 'expected.txt')]
        paths += ['/workspace/tests/cases.json', '/workspace/fixture.txt', '/workspace/expected.txt']
        (source / 'app.py').write_text('import pathlib,sys\nfor p in %r: assert not pathlib.Path(p).exists()\nprint(sys.argv[1])\n' % paths)
        config = read_json(self.config)
        runner = DockerSandbox(context=os.environ.get('MINEVAL_TEST_CONTEXT'))
        report = evaluation.execute_checks(source, self.root / 'probe-workspace', self.root / 'probe-report',
            private, [{'path': 'tests/cases.json', 'stored_path': 'cases.json'}], [], config,
            config['image'], runner)
        self.assertEqual(report['verdict'], 'pass', report)
