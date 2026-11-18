"""Failure diagnostics stay private and preserve evaluator provenance."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from mineval import devlog, model_review
from mineval.artifacts import write_json
from mineval.sandbox import log_agent_failure
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

    def test_qualitative_failure_categories_are_distinct_and_public_error_is_generic(self):
        for reason in ('nonzero_exit', 'timeout', 'oom'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temp:
                store = Mock(root=Path(temp))
                container = Mock(id='review-container', image_id='image')
                container.wait.return_value = reason == 'timeout'
                container.inspect.return_value = {'Running': False, 'ExitCode': 9,
                                                   'OOMKilled': reason == 'oom', 'Error': 'secret'}
                sandbox = Mock()
                sandbox.container.return_value.__enter__ = Mock(return_value=container)
                sandbox.container.return_value.__exit__ = Mock(return_value=False)
                with patch('mineval.sandbox.event') as logged:
                    with self.assertRaisesRegex(ValueError, '^Qualitative review did not complete; retry separately$'):
                        model_review.infer(store, {}, image='local:test', sandbox=sandbox)
                event = next(c for c in logged.call_args_list if c.args[0] == 'qualitative_review.failed')
                self.assertEqual(event.kwargs['reason'], reason)
                self.assertEqual(event.kwargs['sensitive']['state']['Error'], 'secret')
