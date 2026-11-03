"""Phase 7: saved artifacts, independent private checks, no model calls."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import io
import json
import os
import subprocess
import unittest
from unittest.mock import Mock, patch

from mineval import benchmark, evaluation
from mineval.artifacts import inventory, read_json, write_json
from mineval.cli import main
from mineval.preparation import Store
from mineval.sandbox import DockerSandbox, SandboxError
from test_agents import SimulatedDocker
from test_preparation import HistoryFixture


class EvaluationRunner:
    """Run only fixed fixture Python tests locally; production uses rootless Docker."""
    def __init__(self, assessment=None, mode=None, image_id="sha256:fixture"):
        self.assessment, self.mode, self.image_id = assessment, mode, image_id
        self.specs = []

    @contextmanager
    def container(self, spec):
        self.specs.append(spec)
        if self.mode == 'startup-error':
            raise SandboxError('private error text must not be retained')
        container = Mock(id='evaluation-fixture', image_id=self.image_id)
        container.start.side_effect = lambda: (write_json(spec.control / 'assessment.json', self.assessment)
            if spec.control else None)
        container.wait.return_value = False
        container.inspect.return_value = {'Running': True, 'ExitCode': 0}
        if spec.control:
            self.baseline = read_json(spec.inputs / 'baseline-results.json')
            container.inspect.return_value['Running'] = False
            # Evaluator edits to production code must never be used for checking.
            (spec.workspace / 'app.py').write_text('def answer(): return -100\n')
        def execute(argv, **kwargs):
            process = subprocess.run(argv, cwd=spec.workspace, capture_output=True, text=True,
                                     env={'PATH': os.environ['PATH'], 'PYTHONPATH': str(spec.workspace)})
            code = 127 if self.mode == 'command-error' else 1 if self.mode == 'contention' else process.returncode
            return {'resources': {'contended': self.mode == 'contention'}, 'exit_code': code, 'timed_out': self.mode == 'timeout', 'oom_killed': self.mode == 'oom',
                    'stdout': process.stdout, 'stderr': process.stderr, 'logs_truncated': False,
                    'duration_seconds': 0.01}
        container.exec.side_effect = execute
        yield container


class EvaluationTests(HistoryFixture):
    def setUp(self):
        super().setUp()
        self.version = self.freeze('example', self.review)['version']
        self.config = self.root / 'evaluation.json'
        write_json(self.config, {'image': 'local:test', 'network': 'none', 'commands': [
            {'name': 'answer-regression', 'kind': 'tests', 'argv': ['sh', '-c', 'PYTHONPATH=. python3 tests/test_answer.py']}]})

    def saved(self, run_id='candidate', code='def answer(): return 42\n', sandbox=None):
        class Candidate(SimulatedDocker):
            @contextmanager
            def container(inner, spec):
                with super().container(spec) as container:
                    start = container.start.side_effect
                    def write_candidate():
                        start()
                        (spec.workspace / 'app.py').write_text(code)
                        # Candidate's bogus check must be replaced with original evidence.
                        (spec.workspace / 'tests').mkdir(exist_ok=True)
                        (spec.workspace / 'tests/test_answer.py').write_text('assert True\n')
                    container.start.side_effect = write_candidate
                    yield container
        return benchmark.run(self.store, 'example', self.version, run_id=run_id,
                             sandbox=sandbox or Candidate())

    def assess(self, run_id='candidate', evaluation_id=None, runner=None, **kwargs):
        return evaluation.evaluate(self.store, 'example', run_id, self.config,
            evaluation_id=evaluation_id, sandbox=runner or EvaluationRunner(), **kwargs)

    def test_resource_failed_run_is_not_evaluated_as_a_partial_solution(self):
        record = self.saved()
        for kind in ('resource_exhaustion', 'resource_contention'):
            runner = EvaluationRunner()
            with patch('mineval.evaluation.inspect_attempt', return_value=record | {'failure_kind': kind, 'status': 'error'}):
                report = self.assess(evaluation_id=kind, runner=runner)
            self.assertEqual(report['verdict'], 'error')
            self.assertEqual(report['error_kind'], kind)
            self.assertFalse(runner.specs)

    def test_pass_fail_reassessment_and_immutable_inputs_across_restart(self):
        self.saved()
        frozen = self.store.task('example') / 'versions' / self.version
        output = benchmark.output_path(self.store, 'example', 'candidate')
        before = inventory(frozen), inventory(output)
        runner = EvaluationRunner()
        first = self.assess(evaluation_id='first', runner=runner)
        second = self.assess(evaluation_id='second')
        self.assertEqual(first['verdict'], 'pass', first)
        self.assertEqual((first['evaluation_version'], second['evaluation_version']), (1, 2))
        self.assertEqual(Store(self.store.root).inspect_evaluation('example', 'first'), first)
        self.assertEqual((inventory(frozen), inventory(output)), before)
        self.assertEqual(runner.specs[0].readonly_paths, ('tests/test_answer.py',))
        self.assertIsNone(runner.specs[0].control)
        self.assertIsNone(runner.specs[0].inputs)
        evidence = self.store.task('example') / 'evaluation-evidence/first'
        checks = read_json(evidence / 'baseline/selected-checks.json')
        self.assertEqual(checks[0]['commit'], self.head)
        self.assertEqual(checks[0]['sha256'], checks[0]['executed_sha256'])
        self.assertFalse(checks[0]['adapted'])
        self.assertNotIn('transcript', str(inventory(evidence)))
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.assess(evaluation_id='first')
        self.saved('wrong', 'def answer(): return 7\n')
        failed = self.assess('wrong')
        self.assertEqual(failed['verdict'], 'fail')
        self.assertIn('AssertionError', failed['commands'][0]['stderr'])

    def test_passing_checks_do_not_launch_evaluator(self):
        self.saved()
        runner = EvaluationRunner({'deliberately': 'invalid'})
        result = self.assess(runner=runner, command=['must-not-run'])
        self.assertEqual(result['verdict'], 'pass')
        self.assertIsNone(result['evaluator'])
        self.assertEqual(len(runner.specs), 1)

    def test_alternative_interface_adaptation_preserves_assertion_and_records_evidence(self):
        self.saved(code='def result(): return 42\n')
        assessment = {'adaptations': [{'path': 'tests/test_answer.py',
            'content': 'from app import result\nassert result() == 42\n',
            'reason': 'Task specifies the returned value, not the historical function name.',
            'evidence': [{'commit': self.head, 'path': 'tests/test_answer.py'}]}],
            'limitations': ['Fixture adaptation reviewed for behavior preservation.'], 'inconclusive_reason': None}
        runner = EvaluationRunner(assessment)
        result = self.assess(runner=runner, command=['fixture-evaluator'])
        self.assertEqual(result['verdict'], 'pass', result)
        self.assertEqual(result['adaptations'], assessment['adaptations'])
        self.assertFalse(runner.specs[1].retain_logs)
        self.assertEqual(runner.baseline['verdict'], 'fail')
        self.assertNotEqual(runner.specs[1].workspace, runner.specs[2].workspace)
        self.saved('wrong-alternative', 'def result(): return 7\n')
        self.assertEqual(self.assess('wrong-alternative', runner=EvaluationRunner(assessment),
                                    command=['fixture-evaluator'])['verdict'], 'fail')
        assessment['adaptations'][0]['evidence'][0]['commit'] = 'a' * 40
        self.assertEqual(self.assess(runner=EvaluationRunner(assessment), command=['fixture-evaluator'])['verdict'], 'error')

    def test_test_execution_error_can_be_adapted_without_weakening_assertions(self):
        cfg = read_json(self.config)
        cfg['commands'][0]['argv'] = ['python3', '-c',
            "import runpy, sys\ntry:\n runpy.run_path('tests/test_answer.py')\n"
            "except ImportError:\n sys.exit(3)\nexcept AssertionError:\n sys.exit(1)"]
        cfg['commands'][0]['inconclusive_exit_codes'] = [2, 3]
        write_json(self.config, cfg)
        assessment = {'adaptations': [{'path': 'tests/test_answer.py',
            'content': 'from app import result\nassert result() == 42\n',
            'reason': 'Use the alternative interface while preserving the answer requirement.',
            'evidence': [{'commit': self.head, 'path': 'tests/test_answer.py'}]}],
            'limitations': [], 'inconclusive_reason': None}
        for run_id, answer, expected in [('correct-interface', 42, 'pass'), ('wrong-interface', 7, 'fail')]:
            self.saved(run_id, code=f'def result(): return {answer}\n')
            runner = EvaluationRunner(assessment)
            result = self.assess(run_id, runner=runner, command=['fixture-evaluator'])
            self.assertEqual(runner.baseline['verdict'], 'inconclusive')
            self.assertEqual(runner.baseline['commands'][0]['exit_code'], 3)
            self.assertEqual(result['verdict'], expected, result)
            self.assertNotIn('error_kind', result)
            self.assertEqual(len(runner.specs), 3)
            self.assertNotEqual(runner.specs[0].workspace, runner.specs[2].workspace)
            evidence = self.store.task('example') / 'evaluation-evidence' / result['evaluation_id']
            self.assertEqual(read_json(evidence / 'baseline/execution.json')['verdict'], 'inconclusive')
            self.assertEqual(read_json(evidence / 'adapted/execution.json')['verdict'], expected)

        # An adaptation that still cannot import its interface stays inconclusive.
        assessment['adaptations'][0]['content'] = 'from app import missing\nassert missing() == 42\n'
        self.assertEqual(self.assess('correct-interface', runner=EvaluationRunner(assessment),
                                    command=['fixture-evaluator'])['verdict'], 'inconclusive')

    def test_adaptations_cannot_override_infrastructure_failure(self):
        self.saved()
        assessment = {'adaptations': [{'path': 'tests/test_answer.py',
            'content': 'from app import answer\nassert answer() == 42\n',
            'reason': 'Evidence-backed test proposal does not repair infrastructure.',
            'evidence': [{'commit': self.head, 'path': 'tests/test_answer.py'}]}],
            'limitations': [], 'inconclusive_reason': None}
        for mode in ('command-error', 'timeout', 'oom', 'contention'):
            runner = EvaluationRunner(assessment, mode=mode)
            self.assertEqual(self.assess(runner=runner, command=['fixture-evaluator'])['verdict'], 'error')
            self.assertEqual(len(runner.specs), 1)
        cfg = read_json(self.config)
        cfg['commands'].insert(0, {'name': 'setup', 'kind': 'setup', 'argv': ['sh', '-c', 'exit 3']})
        write_json(self.config, cfg)
        runner = EvaluationRunner(assessment)
        self.assertEqual(self.assess(runner=runner, command=['fixture-evaluator'])['verdict'], 'error')
        self.assertEqual(len(runner.specs), 1)

    def test_inconclusive_and_infrastructure_error_are_not_task_failure(self):
        self.saved()
        assessment = {'adaptations': [], 'limitations': ['Cannot reproduce intermittent source requirement'],
                      'inconclusive_reason': 'Original evidence is flaky and cannot establish the behavior.'}
        cfg = read_json(self.config)
        cfg['commands'][0]['argv'] = ['sh', '-c', 'exit 2']
        write_json(self.config, cfg)
        result = self.assess(runner=EvaluationRunner(assessment), command=['fixture-evaluator'])
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertEqual(result['commands'][0]['status'], 'inconclusive')
        for mode in ('startup-error', 'command-error', 'timeout'):
            result = self.assess(runner=EvaluationRunner(mode=mode))
            self.assertEqual(result['verdict'], 'error', result)
            self.assertNotIn('private error text', json.dumps(result))
        benchmark.run(self.store, 'example', self.version, run_id='no-output',
                      sandbox=EvaluationRunner(mode='startup-error'))
        self.assertEqual(self.assess('no-output')['verdict'], 'error')

    def test_command_outcome_contract_and_evaluator_execution_error(self):
        self.saved()
        cfg = read_json(self.config)
        cfg['commands'][0]['argv'] = ['sh', '-c', 'exit 2']
        write_json(self.config, cfg)
        self.assertEqual(self.assess()['verdict'], 'inconclusive')
        cfg['commands'][0]['argv'] = ['sh', '-c', 'exit 9']
        write_json(self.config, cfg)
        self.assertEqual(self.assess()['verdict'], 'error')
        cfg['commands'][0]['failure_exit_codes'] = [9]
        write_json(self.config, cfg)
        self.assertEqual(self.assess()['verdict'], 'fail')
        error = {'adaptations': [], 'limitations': [], 'inconclusive_reason': None,
                 'error_reason': 'Result was an unavailable dependency, not a failed assertion.'}
        self.assertEqual(self.assess(runner=EvaluationRunner(error), command=['fixture'])['verdict'], 'error')
        cfg['commands'][0]['inconclusive_exit_codes'] = [9]
        write_json(self.config, cfg)
        with self.assertRaisesRegex(ValueError, 'must not overlap'):
            self.assess()
        cfg['commands'][0].pop('inconclusive_exit_codes')
        cfg['commands'][0]['expected_failure'] = 'Historical base fails'
        write_json(self.config, cfg)
        with self.assertRaisesRegex(ValueError, 'cannot exempt'):
            self.assess()

    def test_receipts_detect_modified_assessment_evidence_and_saved_outputs(self):
        self.saved()
        result = self.assess(evaluation_id='receipt')
        evidence = self.store.task('example') / 'evaluation-evidence/receipt/report.json'
        evidence.chmod(0o644); evidence.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.store.inspect_evaluation('example', 'receipt')
        self.assess(evaluation_id='output-receipt')
        archive = benchmark.output_path(self.store, 'example', 'candidate') / 'agent/workspace.tar.gz'
        archive.chmod(0o644); archive.write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.store.inspect_evaluation('example', 'output-receipt')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.assess(evaluation_id='not-published')
        self.assertFalse((self.store.task('example') / 'evaluations/not-published.json').exists())

    def test_comparisons_explicit_assessments_and_policy_coverage(self):
        self.saved('left'); self.saved('right', 'def answer(): return 0\n')
        self.assess('left', 'left'); self.assess('right', 'right')
        ref = lambda name: {'task_id': 'example', 'evaluation_id': name}
        definition = {'left': [ref('left')], 'right': [ref('right')]}
        result = evaluation.compare(self.store, definition)
        self.assertEqual(result['coverage']['common'], 1)
        self.assertEqual(result['rates']['left']['pass_rate'], 1)
        self.assertEqual(result['rates']['right']['counts']['fail'], 1)
        uncertain = {'adaptations': [], 'limitations': [], 'inconclusive_reason': 'Insufficient original evidence'}
        self.assess('right', 'uncertain', runner=EvaluationRunner(uncertain), command=['fixture'])
        result = evaluation.compare(self.store, definition | {'right': [ref('uncertain')]})
        self.assertEqual(result['rates']['right']['counts']['inconclusive'], 1)
        self.assertEqual(result['rates']['right']['denominator'], 1)
        self.assertEqual(result['rates']['right']['decided_coverage'], 0)
        self.assess('right', 'different-image', runner=EvaluationRunner(image_id='sha256:changed'))
        with self.assertRaisesRegex(ValueError, 'same resolved verification image'):
            evaluation.compare(self.store, definition | {'right': [ref('different-image')]})
        cfg = read_json(self.config); cfg['network'] = 'bridge'; write_json(self.config, cfg)
        self.assess('right', 'different-policy')
        with self.assertRaisesRegex(ValueError, 'same evaluation policy'):
            evaluation.compare(self.store, definition | {'right': [ref('different-policy')]})
        with self.assertRaisesRegex(ValueError, 'one assessment'):
            evaluation.compare(self.store, definition | {'left': [ref('left'), ref('uncertain')]})
        self.version = self.freeze('example', self.review)['version']
        self.saved('new-revision')
        self.assess('new-revision', 'new-revision')
        empty = evaluation.compare(self.store, definition | {'right': [ref('new-revision')]})
        self.assertEqual(empty['coverage']['common'], 0)
        self.assertIsNone(empty['rates']['left']['pass_rate'])
        self.assertEqual(empty['rates']['right']['denominator'], 0)

    def test_cli_evaluate_inspect_compare_and_verdict_exit_codes(self):
        self.saved()
        with patch('mineval.cli.DockerSandbox', return_value=EvaluationRunner()):
            with redirect_stdout(io.StringIO()) as out:
                code = main(['--data', str(self.store.root), 'evaluate', 'candidate', '--task', 'example',
                             '--config', str(self.config), '--evaluation-id', 'cli'])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())['verdict'], 'pass')
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['--data', str(self.store.root), 'evaluation', 'show', 'example', 'cli']), 0)
        self.assertEqual(json.loads(out.getvalue())['evaluation_id'], 'cli')
        definition = self.root / 'compare-assessments.json'
        ref = {'task_id': 'example', 'evaluation_id': 'cli'}
        write_json(definition, {'left': [ref], 'right': [ref]})
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['--data', str(self.store.root), 'evaluation', 'compare', str(definition)]), 0)
        self.assertEqual(json.loads(out.getvalue())['rates']['left']['pass_rate'], 1)
        process = subprocess.run(['python3', '-m', 'mineval', '--data', str(self.store.root),
                                  'evaluation', 'show', 'example', 'cli'], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)['evaluation_id'], 'cli')
        for verdict, code in [('fail', 1), ('inconclusive', 2), ('error', 125)]:
            with patch('mineval.evaluation.evaluate', return_value={'verdict': verdict}), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--data', str(self.store.root), 'evaluate', 'candidate', '--task', 'example',
                                       '--config', str(self.config)]), code)


@unittest.skipUnless(os.environ.get('MINEVAL_AGENT_TEST_IMAGE'), 'set MINEVAL_AGENT_TEST_IMAGE for live evaluation')
class LiveEvaluationTests(HistoryFixture):
    saved = EvaluationTests.saved
    assess = EvaluationTests.assess

    def setUp(self):
        super().setUp()
        self.version = self.freeze('example', self.review)['version']
        self.config = self.root / 'evaluation.json'
        write_json(self.config, {'image': 'local:test', 'network': 'none', 'commands': [
            {'name': 'answer-regression', 'kind': 'tests', 'argv': ['sh', '-c', 'PYTHONPATH=. python3 tests/test_answer.py']}]})

    def test_live_private_checks_readonly_and_original_artifacts_unchanged(self):
        self.saved()
        image = os.environ['MINEVAL_AGENT_TEST_IMAGE']
        cfg = read_json(self.config)
        cfg['image'] = image
        cfg['commands'].insert(0, {'name': 'protected', 'kind': 'setup', 'argv': ['sh', '-c',
            'if echo "assert True" > tests/test_answer.py 2>/dev/null; then exit 1; fi']})
        cfg['commands'].append({'name': 'protected-after', 'kind': 'setup', 'argv': ['sh', '-c',
            'if rm tests/test_answer.py 2>/dev/null; then exit 1; fi']})
        write_json(self.config, cfg)
        before = inventory(self.store.task('example') / 'versions' / self.version)
        output_before = inventory(benchmark.output_path(self.store, 'example', 'candidate'))
        result = self.assess(runner=DockerSandbox(context=os.environ.get('MINEVAL_TEST_CONTEXT')))
        self.assertEqual(result['verdict'], 'pass', result)
        runner = DockerSandbox(context=os.environ.get('MINEVAL_TEST_CONTEXT'))
        with self.assertRaises(SandboxError):
            runner._cli.call('inspect', result['container_id'])
        self.assertEqual(inventory(self.store.task('example') / 'versions' / self.version), before)
        self.assertEqual(inventory(benchmark.output_path(self.store, 'example', 'candidate')), output_before)
