"""Failure diagnostics stay private and preserve evaluator provenance."""
from contextlib import contextmanager
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from mineval.core import devlog
from mineval.core.artifacts import write_json
from mineval.core.sandbox import log_agent_failure
import test_evaluation as fixtures
from test_preparation import HistoryFixture


class FailingEvaluator(fixtures.EvaluationRunner):
    def __init__(self, reason):
        super().__init__()
        self.reason = reason

    @contextmanager
    def container(self, spec):
        with super().container(spec) as container:
            if spec.control:
                container.wait.return_value = self.reason == 'timeout'
                container.inspect.return_value = {'Running': False, 'ExitCode': 7,
                                                  'OOMKilled': self.reason == 'oom',
                                                  'Error': 'private-state-secret'}
            yield container


class EvaluatorFailureTests(HistoryFixture):
    def setUp(self):
        super().setUp()
        self.fixture_generated_cases = True
        self.fixture_evaluation_config = {'image':'local:test', 'network':'none', 'grading_type':'isolated-argv-v1',
            'commands':[{'name':'answer-regression','kind':'tests',
                         'argv':['python3','-c','from app import answer; print(answer())']}]}
        self.version = self.freeze('example', self.review)['version']
        self.config = self.root / 'evaluation.json'
        write_json(self.config, self.fixture_evaluation_config)

    saved = fixtures.EvaluationTests.saved
    assess = fixtures.EvaluationTests.assess
    def test_failed_evaluator_records_provenance_and_failure_category(self):
        self.saved(code='def answer(): return 7\n')
        runner = FailingEvaluator('nonzero_exit')
        with self.assertRaisesRegex(ValueError, 'no longer supported'):
            self.assess(runner=runner, command=['fixture-evaluator'])
        self.assertEqual(runner.specs, [])
        result = self.assess(runner=runner)
        self.assertEqual(result['verdict'], 'fail')
        self.assertIsNone(result['evaluator'])
        self.assertNotIn('private-state-secret', json.dumps(result))


class PrivateFailureTests(unittest.TestCase):
    def test_state_is_only_in_sensitive_stream(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(devlog, '_streams', {}), \
             patch.object(devlog, '_directory', None):
            root = devlog.configure(temp)
            try:
                log_agent_failure('evaluator.failed', Mock(id='container-id'),
                                  {'Running': False, 'ExitCode': 7, 'Error': 'secret-state'}, False)
                self.assertNotIn('secret-state', (root / 'dev.jsonl').read_text())
                self.assertNotIn('secret-state', (root / 'prod.jsonl').read_text())
                self.assertIn('secret-state', (root / 'sensitive.jsonl').read_text())
            finally:
                for handler in devlog._streams.values():
                    handler.close()
