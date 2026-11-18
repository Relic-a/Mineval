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
    def __init__(self, *, mode=None, image_id="sha256:fixture"):
        self.mode, self.image_id = mode, image_id
        self.specs = []

    @contextmanager
    def container(self, spec):
        self.specs.append(spec)
        if self.mode == 'startup-error':
            raise SandboxError('private error text must not be retained')
        container = Mock(id='evaluation-fixture', image_id=self.image_id)
        container.inspect.return_value = {'Running': True, 'ExitCode': 0}

        def execute(argv, **kwargs):
            process = subprocess.run(argv, cwd=spec.workspace, capture_output=True, text=True,
                                     env={'PATH': os.environ['PATH'], 'PYTHONPATH': str(spec.workspace)})
            code = 127 if self.mode == 'command-error' else 1 if self.mode == 'contention' else 2 if self.mode == 'inconclusive' else process.returncode
            return {'resources': {'contended': self.mode == 'contention'}, 'exit_code': code, 'timed_out': self.mode == 'timeout', 'oom_killed': self.mode == 'oom',
                    'stdout': process.stdout, 'stderr': process.stderr, 'logs_truncated': False,
                    'duration_seconds': 0.01}
        container.exec.side_effect = execute
        yield container


class EvaluationTests(HistoryFixture):
    def setUp(self):
        super().setUp()
        self.fixture_generated_cases = True
        self.fixture_evaluation_config = {'image': 'local:test', 'network': 'none', 'grading_type': 'isolated-argv-v1',
            'commands': [{'name': 'answer-regression', 'kind': 'tests',
                          'argv': ['python3', '-c', 'from pathlib import Path; from app import answer; '
                                   'assert not Path("tests/cases.json").exists(); print(answer())']}]}
        self.version = self.freeze('example', self.review)['version']
        self.config = self.root / 'evaluation.json'
        write_json(self.config, self.fixture_evaluation_config)

    def saved(self, run_id='candidate', code='def answer(): return 42\n', sandbox=None):
        class Candidate(SimulatedDocker):
            @contextmanager
            def container(inner, spec):
                with super().container(spec) as container:
                    start = container.start.side_effect
                    def write_candidate():
                        start()
                        (spec.workspace / 'app.py').write_text(code)
                        # Candidate-supplied tests cannot override the fixed grader.
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
        for kind in ('resource_exhaustion',):
            runner = EvaluationRunner()
            with patch('mineval.evaluation.inspect_attempt', return_value=record | {'failure_kind': kind, 'status': 'error'}):
                report = self.assess(evaluation_id=kind, runner=runner)
            self.assertEqual(report['verdict'], 'error')
            self.assertEqual(report['error_kind'], kind)
            self.assertFalse(runner.specs)

    def test_legacy_contention_rechecks_actual_timeout(self):
        record = self.saved()
        for timed_out, verdict in ((False, 'pass'), (True, 'error')):
            runner = EvaluationRunner()
            legacy = record | {'failure_kind': 'resource_contention', 'status': 'error',
                               'agent_result': {'timed_out': timed_out}}
            with patch('mineval.evaluation.inspect_attempt', return_value=legacy):
                report = self.assess(evaluation_id=f'legacy-{timed_out}', runner=runner)
            self.assertEqual(report['verdict'], verdict)
            self.assertEqual(bool(runner.specs), not timed_out)

    def test_pressure_alone_does_not_override_failed_check(self):
        self.saved()
        report = self.assess(runner=EvaluationRunner(mode='contention'))
        self.assertEqual(report['commands'][0]['status'], 'failed')
        self.assertEqual(report['verdict'], 'fail')

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
        self.assertEqual(runner.specs[0].readonly_paths, ())
        self.assertIsNone(runner.specs[0].control)
        self.assertIsNone(runner.specs[0].inputs)
        evidence = self.store.task('example') / 'evaluation-evidence/first'
        checks = read_json(evidence / 'baseline/selected-checks.json')
        self.assertEqual(checks[0]['origin'], 'generated')
        self.assertEqual(checks[0]['sha256'], checks[0]['executed_sha256'])
        self.assertFalse(checks[0]['adapted'])
        self.assertNotIn('transcript', str(inventory(evidence)))
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.assess(evaluation_id='first')
        self.saved('wrong', 'def answer(): return 7\n')
        failed = self.assess('wrong')
        self.assertEqual(failed['verdict'], 'fail')
        self.assertEqual(failed['commands'][0]['stdout'], '7\n')

    def test_missing_entrypoint_fails_the_fixed_command(self):
        self.saved(code='VALUE = 0\n')
        result = self.assess(evaluation_id='source-failure')
        self.assertEqual(result['verdict'], 'fail')
        self.assertIn('ImportError', result['commands'][0]['stderr'])
        self.assertNotIn('failure_basis', result)
        evidence = self.store.task('example') / 'evaluation-evidence/source-failure'
        self.assertFalse((evidence / 'adapted').exists())

    def test_removed_evaluator_arguments_are_rejected_without_a_record(self):
        self.saved()
        for argument in ({'command': ['fixture']}, {'adapter': object()}, {'auth_file': 'unused'}):
            with self.subTest(argument=argument), self.assertRaisesRegex(ValueError, 'no longer supported'):
                self.assess(**argument)
        self.assertFalse(list((self.store.task('example') / 'evaluations').glob('*.json')))

    def test_output_mismatch_records_failure_without_source_assessment(self):
        self.saved(code='def answer(): return 7\n')
        result = self.assess()
        self.assertEqual(result['verdict'], 'fail')
        self.assertNotIn('candidate_evidence', result)
        self.assertNotIn('failure_basis', result)

    def test_passing_checks_do_not_launch_evaluator(self):
        self.saved()
        runner = EvaluationRunner()
        with self.assertRaisesRegex(ValueError, 'no longer supported'):
            self.assess(runner=runner, command=['must-not-run'])
        self.assertEqual(runner.specs, [])
        self.assertEqual(self.assess(runner=runner)['verdict'], 'pass')
        self.assertEqual(len(runner.specs), 1)

    def test_alternative_interface_does_not_adapt_frozen_checks(self):
        self.saved(code='def result(): return 42\n')
        frozen = self.store.task('example') / 'versions' / self.version
        before = inventory(frozen)
        with self.assertRaisesRegex(ValueError, 'no longer supported'):
            self.assess(command=['fixture-evaluator'])
        result = self.assess()
        self.assertEqual(result['verdict'], 'fail')
        self.assertEqual(result['adaptations'], [])
        self.assertEqual(inventory(frozen), before)

    def test_rejects_replacing_the_frozen_command(self):
        self.saved(code='def result(): return 42\n')
        config = read_json(self.config)
        config['commands'][0]['argv'] = ['python3', '-c', 'print(42)']
        write_json(self.config, config)
        with self.assertRaisesRegex(ValueError, 'differs from the frozen'):
            self.assess()
        write_json(self.config, self.fixture_evaluation_config)
        self.assertEqual(self.assess()['verdict'], 'fail')

    def test_adaptations_cannot_override_infrastructure_failure(self):
        self.saved()
        for mode in ('command-error', 'timeout', 'oom'):
            runner = EvaluationRunner(mode=mode)
            self.assertEqual(self.assess(runner=runner)['verdict'], 'error')
            self.assertEqual(len(runner.specs), 1)
        with self.assertRaisesRegex(ValueError, 'no longer supported'):
            self.assess(command=['fixture-evaluator'])

    def test_inconclusive_and_infrastructure_error_are_not_task_failure(self):
        self.saved()
        result = self.assess(runner=EvaluationRunner(mode='inconclusive'))
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertEqual(result['commands'][0]['status'], 'inconclusive')
        for mode in ('startup-error', 'command-error', 'timeout'):
            result = self.assess(runner=EvaluationRunner(mode=mode))
            self.assertEqual(result['verdict'], 'error', result)
            self.assertNotIn('private error text', json.dumps(result))
        benchmark.run(self.store, 'example', self.version, run_id='no-output',
                      sandbox=EvaluationRunner(mode='startup-error'))
        self.assertEqual(self.assess('no-output')['verdict'], 'error')

    def test_fixed_command_outcome_and_configuration_contract(self):
        self.saved()
        self.assertEqual(self.assess(runner=EvaluationRunner(mode='inconclusive'))['verdict'], 'inconclusive')
        self.assertEqual(self.assess(runner=EvaluationRunner(mode='contention'))['verdict'], 'fail')
        self.assertEqual(self.assess(runner=EvaluationRunner(mode='command-error'))['verdict'], 'error')
        cfg = read_json(self.config)
        cfg['commands'][0]['failure_exit_codes'] = [2]
        write_json(self.config, cfg)
        with self.assertRaisesRegex(ValueError, 'must not overlap'):
            self.assess()
        cfg['commands'][0].pop('failure_exit_codes')
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
        self.assess('right', 'uncertain', runner=EvaluationRunner(mode='inconclusive'))
        result = evaluation.compare(self.store, definition | {'right': [ref('uncertain')]})
        self.assertEqual(result['rates']['right']['counts']['inconclusive'], 1)
        self.assertEqual(result['rates']['right']['denominator'], 1)
        self.assertEqual(result['rates']['right']['decided_coverage'], 0)
        self.assess('right', 'different-image', runner=EvaluationRunner(image_id='sha256:changed'))
        with self.assertRaisesRegex(ValueError, 'same resolved verification image'):
            evaluation.compare(self.store, definition | {'right': [ref('different-image')]})
        cfg = read_json(self.config); cfg['network'] = 'bridge'; write_json(self.config, cfg)
        with self.assertRaisesRegex(ValueError, 'differs from the frozen'):
            self.assess('right', 'different-policy')
        write_json(self.config, self.fixture_evaluation_config)
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
        self.fixture_generated_cases = True
        self.fixture_evaluation_config = {'image': os.environ['MINEVAL_AGENT_TEST_IMAGE'], 'network': 'none', 'grading_type': 'isolated-argv-v1',
            'commands': [{'name': 'answer-regression', 'kind': 'tests',
                          'argv': ['python3', '-c', "from pathlib import Path; assert not Path('/workspace/tests/cases.json').exists(); from app import answer; print(answer())"]}]}
        self.version = self.freeze('example', self.review)['version']
        self.config = self.root / 'evaluation.json'
        write_json(self.config, self.fixture_evaluation_config)

    def test_live_private_checks_readonly_and_original_artifacts_unchanged(self):
        self.saved()
        before = inventory(self.store.task('example') / 'versions' / self.version)
        output_before = inventory(benchmark.output_path(self.store, 'example', 'candidate'))
        result = self.assess(runner=DockerSandbox(context=os.environ.get('MINEVAL_TEST_CONTEXT')))
        self.assertEqual(result['verdict'], 'pass', result)
        runner = DockerSandbox(context=os.environ.get('MINEVAL_TEST_CONTEXT'))
        with self.assertRaises(SandboxError):
            runner._cli.call('inspect', result['container_id'])
        self.assertEqual(inventory(self.store.task('example') / 'versions' / self.version), before)
        self.assertEqual(inventory(benchmark.output_path(self.store, 'example', 'candidate')), output_before)
