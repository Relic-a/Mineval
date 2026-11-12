"""Phase 5: later private regressions, independent verdicts and immutable approval."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, MagicMock

from mineval.artifacts import digest, inventory, read_json, write_json
from mineval.review import approved, checked, session, validate
from mineval.sandbox import DockerSandbox
from mineval.verification import diagnose
from test_preparation import HistoryFixture, FakeRunner, step
from review_helpers import ReportAgent, plan, report


class ReviewTests(HistoryFixture):
    def setUp(self):
        super().setUp()
        # Evidence genuinely follows the solution, rather than being part of it.
        (self.repo / 'tests/later.py').write_text('from app import answer\nassert answer() == 42\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'later regression')
        self.later = self.git('rev-parse', 'HEAD')
        self.store.register(self.candidate | {'task_id': 'later', 'future_evidence': [
            {'commit': self.later, 'path': 'tests/later.py', 'proves': 'correct answer'}]})
        self.approval_id = self.prepare_review('example', self.review)
        self.approval, _ = approved(self.store, 'example', self.approval_id)
        self.extraction_id = self.approval['inputs']['extraction_id']

    def extract(self, selection):
        return session(self.store, 'example', stage='extraction', command=['fake-extractor'],
                       sandbox=ReportAgent('selection.json', selection))

    def review_extraction(self, extraction_id, verdict='approve', reasons=None):
        response = report(self.review) | {'verdict': verdict}
        if reasons:
            response['reasons'] = reasons
        return session(self.store, 'example', stage='review', extraction_id=extraction_id,
                       command=['fake-reviewer'], sandbox=ReportAgent('review.json', response))

    def test_private_agent_copies_cannot_change_draft_or_selected_original_bytes(self):
        before = inventory(self.workspace)
        class EditingAgent(ReportAgent):
            @contextmanager
            def container(agent_self, spec):
                self.assertFalse(spec.retain_logs)
                self.assertIsNone(spec.broker)
                self.assertNotEqual(spec.workspace, self.workspace)
                self.assertTrue((spec.inputs / 'prompt.txt').is_file())
                (spec.workspace / 'cleaned/app.py').write_text('leaked solution')
                (spec.workspace / 'future' / self.head / 'tests/test_answer.py').write_text('assert True')
                with super().container(spec) as agent:
                    yield agent
        result = session(self.store, 'example', stage='extraction', command=['fake-extractor'],
                         sandbox=EditingAgent('selection.json', plan(self.head)))
        extraction = self.store.task('example') / 'private/extractions' / result['extraction_id']
        selected = extraction / 'verification' / self.head / 'tests/test_answer.py'
        self.assertEqual(selected.read_text(), 'from app import answer\nassert answer() == 42\n')
        self.assertEqual(inventory(self.workspace), before)

    def test_private_evidence_index_lists_verified_paths_and_configs(self):
        config = {'image': 'local:test', 'commands': [
            {'name': 'private', 'kind': 'tests', 'argv': ['python3', 'tests/test_answer.py']}]}
        extraction = self.extract(plan(self.head) | {'evaluation_config': config})
        config_path = self.root / 'index-validation.json'
        write_json(config_path, config)
        validation = validate(self.store, 'example', extraction['extraction_id'], config_path,
                              sandbox=FakeRunner([step(1)]))
        class IndexAgent(ReportAgent):
            @contextmanager
            def container(agent_self, spec):
                index = read_json(spec.workspace / 'evidence-index.json')
                tree = next(t for t in index['future_trees'] if t['commit'] == self.head)
                original = '/workspace/future/' + self.head + '/tests/test_answer.py'
                self.assertIn(original, tree['files'])
                self.assertTrue((spec.workspace / original.removeprefix('/workspace/')).is_file())
                details = index['extraction']
                self.assertEqual(details['evaluation_config'], config)
                self.assertTrue(details['evaluation_config_location'].endswith('extraction-manifest.json#/plan/evaluation_config'))
                self.assertEqual(index['diagnostic_runtime_config'], '/workspace/runtime.json')
                self.assertEqual(details['artifacts'][0]['future_original'], original)
                self.assertTrue((spec.workspace / details['artifacts'][0]['selected_copy'].removeprefix('/workspace/')).is_file())
                self.assertIn(validation['validation_id'], index['validations'][0]['record'])
                self.assertTrue(index['validations'][0]['configuration_location'].endswith('#/report/configuration'))
                self.assertIn('evidence-index.json', (spec.inputs / 'prompt.txt').read_text())
                with super().container(spec) as agent:
                    yield agent
        verdict = session(self.store, 'example', stage='review', extraction_id=extraction['extraction_id'],
                          command=['review'], sandbox=IndexAgent('review.json', report(self.review) | {'verdict': 'revise'}))
        class RevisionIndexAgent(ReportAgent):
            @contextmanager
            def container(agent_self, spec):
                index = read_json(spec.workspace / 'evidence-index.json')
                self.assertEqual(index['revision']['review'], '/workspace/revision/review.json')
                self.assertEqual(index['revision']['extraction']['evaluation_config'], config)
                self.assertIn(validation['validation_id'], index['revision']['validations'][0]['record'])
                self.assertIn('evidence-index.json', (spec.inputs / 'prompt.txt').read_text())
                with super().container(spec) as agent:
                    yield agent
        session(self.store, 'example', stage='extraction', revision_id=verdict['review_id'],
                command=['extract'], sandbox=RevisionIndexAgent('selection.json', plan(self.head)))
        self.assertFalse((self.workspace / 'evidence-index.json').exists())

    def test_corrupt_evidence_rejected_before_index_is_used(self):
        task = self.store.task('example')
        source = task / 'private/future' / self.head / 'tests/test_answer.py'
        source.chmod(0o644)
        source.write_text('changed')
        agent = ReportAgent('selection.json', plan(self.head))
        with self.assertRaisesRegex(ValueError, 'Stored source evidence was modified'):
            session(self.store, 'example', stage='extraction', command=['extract'], sandbox=agent)
        self.assertEqual(agent.specs, [])
        manifest = task / 'private/extractions' / self.extraction_id / 'extraction-manifest.json'
        manifest.chmod(0o644)
        manifest.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.review_extraction(self.extraction_id)

    def test_required_config_is_checked_before_sealing_extraction(self):
        task = self.store.task('example')
        before = set((task / 'private/extractions').iterdir())
        with self.assertRaisesRegex(ValueError, 'requires an evaluation_config'):
            session(self.store, 'example', stage='extraction', command=['extract'],
                    require_evaluation_config=True,
                    sandbox=ReportAgent('selection.json', plan(self.head)))
        self.assertEqual(set((task / 'private/extractions').iterdir()), before)
        config = {'image': 'local:test', 'commands': [
            {'name': 'private', 'kind': 'tests', 'argv': ['python3', 'tests/test_answer.py']}]}
        result = session(self.store, 'example', stage='extraction', command=['extract'],
                         require_evaluation_config=True,
                         sandbox=ReportAgent('selection.json', plan(self.head) | {'evaluation_config': config}))
        manifest = read_json(task / 'private/extractions' / result['extraction_id'] / 'extraction-manifest.json')
        self.assertEqual(manifest['plan']['evaluation_config'], config)
        unavailable = plan(self.head) | {'strategy': 'unavailable', 'artifacts': []}
        result = session(self.store, 'example', stage='extraction', command=['extract'],
                         require_evaluation_config=True,
                         sandbox=ReportAgent('selection.json', unavailable))
        self.assertEqual(result['status'], 'needs-review')
        result = session(self.store, 'example', stage='extraction', command=['extract'],
                         require_evaluation_config=True,
                         sandbox=ReportAgent('selection.json', unavailable | {'evaluation_config': None}))
        self.assertEqual(result['status'], 'needs-review')

    def test_inline_attestation_and_legacy_extraction_cannot_approve(self):
        with self.assertRaisesRegex(ValueError, 'independent review'):
            self.store.finalize('example', self.review)
        legacy = self.store.extract('example', plan(self.head)['artifacts'])
        with self.assertRaisesRegex(ValueError, 'separate extraction'):
            self.review_extraction(legacy['extraction_id'])
        self.assertNotEqual(self.approval['producer']['session_id'],
            read_json(self.store.task('example') / 'private/extractions' / self.extraction_id /
                      'extraction-manifest.json')['producer_session']['session_id'])

    def test_reviewer_flags_future_production_and_superficial_checks(self):
        for path, reason in [('app.py', 'Future production implementation is not test support'),
                             ('tests/test_answer.py', 'Check assumes an arbitrary answer; candidate needs clearer requirements')]:
            extraction = self.extract(plan(self.head, path))
            result = self.review_extraction(extraction['extraction_id'], 'revise', [reason])
            self.assertEqual(result['reasons'], [reason])
            with self.assertRaisesRegex(ValueError, 'did not approve'):
                self.store.finalize('example', review_id=result['review_id'])
        result = self.review_extraction(self.extraction_id, 'reject', ['Task cannot be verified objectively'])
        with self.assertRaisesRegex(ValueError, 'did not approve'):
            self.store.finalize('example', review_id=result['review_id'])

    def test_revision_feedback_restarts_extraction_and_survives_interruption(self):
        from mineval.checkpoints import PreparationCheckpoints
        task = self.store.task('example')
        config = self.root / 'private-check.json'
        write_json(config, {'image': 'local:test', 'commands': [
            {'name': 'regression', 'kind': 'tests', 'argv': ['python3', 'tests/test_answer.py']}]})
        validation = validate(self.store, 'example', self.extraction_id, config,
                              sandbox=FakeRunner([step(1)]))
        verdict = self.review_extraction(self.extraction_id, 'revise', ['Command timed out'])
        progress = PreparationCheckpoints(self.store, 'example', lambda image: image)
        # Mimic an old checkpoint written before retry feedback was implemented.
        progress.saved['extraction'] = {'key': 'old', 'value': {'extraction_id': self.extraction_id}}
        write_json(progress.path, progress.saved)
        self.assertEqual(progress.revision(), verdict['review_id'])
        class RevisionAgent(ReportAgent):
            @contextmanager
            def container(agent_self, spec):
                revision = spec.workspace / 'revision'
                self.assertEqual(read_json(revision / 'review.json')['report']['reasons'], ['Command timed out'])
                self.assertTrue((revision / 'extraction/extraction-manifest.json').exists())
                self.assertTrue((revision / 'validations' / validation['validation_id'] / 'validation.json').exists())
                with super().container(spec) as agent:
                    yield agent
        def extract():
            return session(self.store, 'example', stage='extraction', command=['extract'],
                           revision_id=verdict['review_id'], sandbox=RevisionAgent('selection.json', plan(self.head)))
        new = progress.run('extraction', ['revision', progress.revision()], extract, progress.extraction)
        self.assertNotEqual(new['extraction_id'], self.extraction_id)
        manifest = read_json(task / 'private/extractions' / new['extraction_id'] / 'extraction-manifest.json')
        self.assertEqual(manifest['producer_session']['revision_review_id'], verdict['review_id'])
        restarted = PreparationCheckpoints(self.store, 'example', lambda image: image)
        self.assertEqual(restarted.revision(), verdict['review_id'])
        self.assertEqual(restarted.run('extraction', ['revision', restarted.revision()],
                                      lambda: self.fail('repeated extraction'), restarted.extraction), new)
        diagnose(self.store, 'example', sandbox=FakeRunner([step()]))
        self.assertIsNone(restarted.revision())
        with self.assertRaisesRegex(ValueError, 'stale'):
            extract()

    def test_unavailable_and_failed_agents_never_publish_approval(self):
        result = self.extract(plan(self.head) | {'strategy': 'unavailable', 'artifacts': [],
                                               'rationale': 'No defensible objective verification'})
        self.assertEqual(result['status'], 'needs-review')
        for state in ({'Running': False, 'ExitCode': 7},
                      {'Running': False, 'ExitCode': 0, 'OOMKilled': True}):
            runner = MagicMock()
            agent = runner.container.return_value.__enter__.return_value
            agent.wait.return_value = False
            agent.inspect.return_value = state
            with self.assertRaisesRegex(ValueError, 'did not complete'):
                session(self.store, 'example', stage='review', extraction_id=self.extraction_id,
                        command=['failed'], sandbox=runner)

    def test_approval_staleness_tamper_and_new_revisions(self):
        first = self.store.finalize('example', review_id=self.approval_id)
        frozen = self.store.task('example') / 'versions' / first['version']
        before = inventory(frozen)
        self.store.materialize('example', first['version'], self.root / 'public')
        self.assertFalse((self.root / 'public/tests').exists())
        self.assertFalse((self.root / 'public/private').exists())
        self.assertTrue((frozen / 'private/verification/extraction-manifest.json').exists())
        diagnose(self.store, 'example', sandbox=FakeRunner([step()]))
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.store.finalize('example', review_id=self.approval_id)
        second_review = self.review_extraction(self.extraction_id)
        second = self.store.finalize('example', review_id=second_review['review_id'])
        self.assertNotEqual(first['version'], second['version'])
        self.assertEqual(inventory(frozen), before)
        extraction = self.store.task('example') / 'private/extractions' / self.extraction_id
        path = extraction / 'extraction-manifest.json'
        path.chmod(0o644); path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.store.finalize('example', review_id=second_review['review_id'])

    def test_validation_is_private_distinguishes_failures_and_invalidates_review(self):
        config = self.root / 'private-check.json'
        write_json(config, {'image': 'local:test', 'commands': [
            {'name': 'regression', 'kind': 'tests', 'argv': ['python3', 'tests/test_answer.py']}]})
        latest = digest(self.store.task('example') / 'verify-latest.json')
        for result, interpretation in [(step(1), 'inconclusive'), (step(2), 'inconclusive'),
                                       (step(127), 'environment_failure'),
                                       (step(), 'passed')]:
            outcome = validate(self.store, 'example', self.extraction_id, config,
                               future_commit=self.head if interpretation == 'passed' else None,
                               sandbox=FakeRunner([result]))
            self.assertEqual(outcome['interpretation'], interpretation)
        self.assertEqual(digest(self.store.task('example') / 'verify-latest.json'), latest)
        self.assertFalse((self.workspace / 'tests').exists())
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.store.finalize('example', review_id=self.approval_id)
        approval = self.review_extraction(self.extraction_id)
        frozen = self.store.finalize('example', review_id=approval['review_id'])
        self.assertTrue((self.store.task('example') / 'versions' / frozen['version'] / 'private/validations').exists())

    def test_validation_assesses_and_reruns_only_selected_test_on_fresh_copy(self):
        from test_evaluation import EvaluationRunner
        config = self.root / 'private-adapt.json'
        write_json(config, {'image': 'local:test', 'commands': [
            {'name': 'private', 'kind': 'tests', 'argv': ['python3', 'tests/test_answer.py']}]})
        (self.workspace / 'app.py').write_text('def result(): return 42\n')
        assessment = {'adaptations': [{'path': 'tests/test_answer.py',
            'content': 'from app import result\nassert result() == 42\n',
            'reason': 'Use the existing alternative function.',
            'evidence': [{'commit': self.head, 'path': 'tests/test_answer.py'}]}],
            'limitations': [], 'inconclusive_reason': None}
        before = inventory(self.store.task('example') / 'private/extractions' / self.extraction_id)
        class InspectInputRunner(EvaluationRunner):
            @contextmanager
            def container(inner, spec):
                if spec.control:
                    task_input = read_json(spec.inputs / 'task.json')
                    self.assertEqual(task_input, {'task_summary': read_json(
                        self.store.task('example') / 'metadata.json')['task_summary']})
                    self.assertIsInstance(task_input['task_summary'], str)
                with super(InspectInputRunner, inner).container(spec) as agent:
                    yield agent
        runner = InspectInputRunner(assessment)
        result = validate(self.store, 'example', self.extraction_id, config,
                          sandbox=runner, command=['fixture-evaluator'])
        self.assertEqual(result['interpretation'], 'passed')
        self.assertEqual(result['baseline']['verdict'], 'fail')
        self.assertEqual(result['adapted']['verdict'], 'pass')
        self.assertEqual(result['assessment'], assessment)
        self.assertEqual(result['evaluator']['harness'], 'explicit-command')
        self.assertNotEqual(runner.specs[0].workspace, runner.specs[2].workspace)
        self.assertEqual((self.workspace / 'app.py').read_text(), 'def result(): return 42\n')
        self.assertEqual(inventory(self.store.task('example') / 'private/extractions' / self.extraction_id), before)
        evidence = self.store.task('example') / 'private/validations' / self.extraction_id / result['validation_id']
        self.assertFalse(read_json(evidence / 'baseline/selected-checks.json')[0]['adapted'])
        adapted_checks = read_json(evidence / 'adapted/selected-checks.json')
        self.assertTrue(adapted_checks[0]['adapted'])
        self.assertEqual(result['report']['selected_checks'], adapted_checks)
        self.assertNotEqual(adapted_checks[0]['executed_sha256'],
                            read_json(evidence / 'baseline/selected-checks.json')[0]['executed_sha256'])
        self.assertEqual(read_json(evidence / 'assessment.json'), assessment)
        checked(evidence)

    def test_validation_rejects_production_adaptation_and_preserves_failures(self):
        from test_evaluation import EvaluationRunner
        config = self.root / 'private-invalid.json'
        write_json(config, {'image': 'local:test', 'commands': [
            {'name': 'private', 'kind': 'tests', 'argv': ['python3', 'tests/test_answer.py']}]})
        proposal = {'adaptations': [{'path': 'app.py', 'content': 'def answer(): return 42\n',
            'reason': 'Invent missing behavior', 'evidence': [{'commit': self.head, 'path': 'tests/test_answer.py'}]}],
            'limitations': [], 'inconclusive_reason': None}
        runner = EvaluationRunner(proposal)
        result = validate(self.store, 'example', self.extraction_id, config, sandbox=runner, command=['assess'])
        self.assertEqual(result['interpretation'], 'environment_failure')
        self.assertEqual(result['baseline']['verdict'], 'fail')
        self.assertEqual(len(runner.specs), 2)
        evidence = self.store.task('example') / 'private/validations' / self.extraction_id / result['validation_id']
        self.assertEqual(read_json(evidence / 'assessment.json'), proposal)
        class TimeoutAssessment(EvaluationRunner):
            @contextmanager
            def container(inner, spec):
                with super(TimeoutAssessment, inner).container(spec) as container:
                    if spec.control:
                        container.wait.return_value = True
                    yield container
        result = validate(self.store, 'example', self.extraction_id, config,
                          sandbox=TimeoutAssessment(proposal), command=['assess'])
        self.assertEqual(result['interpretation'], 'environment_failure')
        self.assertEqual(result['report']['error_kind'], 'TimeoutError')
        self.assertEqual(result['baseline']['verdict'], 'fail')
        self.assertEqual(result['evaluator']['harness'], 'explicit-command')
        no_alternative = {'adaptations': [], 'limitations': ['Missing implementation is not an alternative.'],
                          'inconclusive_reason': 'Cannot establish behavior from the harness failure.'}
        result = validate(self.store, 'example', self.extraction_id, config,
                          sandbox=EvaluationRunner(no_alternative), command=['assess'])
        self.assertEqual(result['interpretation'], 'inconclusive')
        for mode in ('timeout', 'command-error'):
            runner = EvaluationRunner(proposal, mode=mode)
            result = validate(self.store, 'example', self.extraction_id, config, sandbox=runner, command=['assess'])
            self.assertEqual(result['interpretation'], 'environment_failure')
            self.assertEqual(len(runner.specs), 1)
            self.assertEqual(result['baseline']['verdict'], 'error')

    def test_review_can_record_unexecutable_base_as_a_limitation(self):
        from test_evaluation import EvaluationRunner
        from mineval.review import EXTRACTION, REVIEW, validate_review
        config = self.root / 'private-collection.json'
        write_json(config, {'image': 'local:test', 'commands': [{
            'name': 'private', 'kind': 'tests', 'argv': ['python3', '-c',
                "import app, runpy\nif app.answer() == 0: import absent_fixture_interface\nrunpy.run_path('tests/test_answer.py')"]}]})
        assessment = {'adaptations': [], 'limitations': ['No existing equivalent base interface'],
                      'inconclusive_reason': None,
                      'error_reason': 'Test collection cannot import the missing base interface.'}
        base = validate(self.store, 'example', self.extraction_id, config,
                        command=['fixture-assessor'], sandbox=EvaluationRunner(assessment))
        future = validate(self.store, 'example', self.extraction_id, config,
                          future_commit=self.head, sandbox=EvaluationRunner())
        self.assertEqual(base['interpretation'], 'environment_failure')
        self.assertEqual(base['baseline']['verdict'], 'fail')
        self.assertIn('ModuleNotFoundError', base['baseline']['commands'][0]['stderr'])
        self.assertEqual(base['assessment'], assessment)
        self.assertIsNone(base['adapted'])
        self.assertEqual(future['interpretation'], 'passed')
        self.assertIn('record that base assertions\ndid not run', EXTRACTION)
        self.assertIn('never claim base assertions ran', REVIEW)
        self.assertIn('A missing module\nalone does not establish absence', REVIEW)
        self.assertIn('future environment failures remain blockers', REVIEW)
        class InspectReviewer(ReportAgent):
            @contextmanager
            def container(agent_self, spec):
                index = read_json(spec.workspace / 'evidence-index.json')
                records = [read_json(spec.workspace / item['record'].removeprefix('/workspace/'))
                           for item in index['validations']]
                self.assertTrue(any(r['interpretation'] == 'environment_failure' and
                                    r['assessment'] == assessment for r in records))
                self.assertTrue(any(r['interpretation'] == 'passed' and r['state'] == self.head
                                    for r in records))
                self.assertIn('never claim base assertions ran', (spec.inputs / 'prompt.txt').read_text())
                with super().container(spec) as agent:
                    yield agent
        reviewer = InspectReviewer('review.json', report(self.review) | {
            'reasons': ['Future checks ran; base collection failed and no base assertions ran.']})
        result = session(self.store, 'example', stage='review', extraction_id=self.extraction_id,
                         command=['fixture-reviewer'], sandbox=reviewer)
        self.assertEqual(result['verdict'], 'approve')
        with self.assertRaisesRegex(ValueError, 'explicit integrity'):
            validate_review(report(self.review) | {'snapshot_reviewed': False})

    def test_validation_accepts_explicit_source_assessed_failure(self):
        from test_evaluation import EvaluationRunner
        config = self.root / 'source-assessment.json'
        write_json(config, {'image': 'local:test', 'commands': [
            {'name': 'private', 'kind': 'tests', 'argv': ['python3', 'tests/test_answer.py']}]})
        assessment = {'adaptations': [], 'limitations': ['No assertion completed.'],
                      'inconclusive_reason': None,
                      'behavior_failure_reason': 'Source inspection found no correct behavior or alternate interface.',
                      'candidate_evidence': [{'path': 'app.py', 'reason': 'The existing answer returns the wrong value.'}]}
        result = validate(self.store, 'example', self.extraction_id, config,
                          command=['assessor'], sandbox=EvaluationRunner(assessment))
        self.assertEqual(result['interpretation'], 'behavior_failure')
        self.assertEqual(result['report']['failure_basis'], 'source_assessment')
        self.assertEqual(result['assessment'], assessment)
        self.assertEqual(result['report']['candidate_evidence'], assessment['candidate_evidence'])

    def test_validation_executes_selected_fixture_on_base_and_future(self):
        from test_evaluation import EvaluationRunner
        config = self.root / 'private-real.json'
        write_json(config, {'image': 'local:test', 'network': 'none', 'commands': [
            {'name': 'private', 'kind': 'tests', 'argv': ['python3', 'tests/test_answer.py']}]})
        base = validate(self.store, 'example', self.extraction_id, config, sandbox=EvaluationRunner())
        future = validate(self.store, 'example', self.extraction_id, config,
                          future_commit=self.head, sandbox=EvaluationRunner())
        self.assertEqual(base['interpretation'], 'inconclusive')
        self.assertEqual(future['interpretation'], 'passed')
        self.assertEqual(base['report']['configuration'], read_json(config))
        self.assertEqual(base['report']['selected_checks'][0]['path'], 'tests/test_answer.py')
        self.assertFalse((self.workspace / 'tests').exists())

    @unittest.skipUnless(os.environ.get('MINEVAL_TEST_IMAGE'), 'set MINEVAL_TEST_IMAGE for live private sessions')
    def test_live_later_regression_isolated_extraction_review_and_checks(self):
        # Use the normal fixture cleanup producer, then real Docker for phase 5.
        # The later task has no cleanup yet; record an ordinary no-edit producer.
        from mineval.cleanup import save_attempt
        from mineval.sandbox import RunResult
        import tempfile
        target_config = self.store.draft('later') / 'control/verify.json'
        write_json(target_config, {'image': os.environ['MINEVAL_TEST_IMAGE'], 'commands': [
            {'name': 'base', 'kind': 'smoke', 'argv': ['python3', '-c', 'import app']}]})
        runner = DockerSandbox(context=os.environ.get('MINEVAL_TEST_CONTEXT'))
        diagnostic = diagnose(self.store, 'later', sandbox=runner)
        self.assertEqual(diagnostic['status'], 'completed')
        with self.store.operation('later'), tempfile.TemporaryDirectory(dir=self.store.root) as temp:
            from mineval.artifacts import copy_tree
            temp = Path(temp)
            copy_tree(self.store.draft('later') / 'workspace', temp / 'clean')
            control = temp / 'control'; control.mkdir()
            write_json(control / 'verify.json', read_json(target_config))
            write_json(control / 'cleanup-report.json', {'disposition': 'ready-for-review',
                'summary': 'No edits', 'objective_preserved': True, 'changes': [], 'environment': {
                    'runtime': 'Python fixture image', 'dependencies': [], 'services': [], 'limitations': []}})
            save_attempt(self.store, 'later', temp / 'clean', control,
                RunResult('fixture', 'image', 0, False, False, '', '', False, 0), image_id='image',
                diagnostic_ids=[diagnostic['diagnostic_id']], producer={'harness': 'fixture'},
                input_hashes=inventory(temp / 'clean'), instructions_sha256='fixture')
        import json
        def command(filename, value):
            return ['python3', '-c', 'import pathlib; pathlib.Path(' + repr('/mineval-control/' + filename) +
                    ').write_text(' + repr(json.dumps(value)) + ')']
        extraction = session(self.store, 'later', stage='extraction', sandbox=runner,
            command=command('selection.json', plan(self.later, 'tests/later.py')))
        config = self.root / 'private-live.json'
        write_json(config, {'image': os.environ['MINEVAL_TEST_IMAGE'], 'commands': [
            {'name': 'regression', 'kind': 'tests', 'argv': ['/bin/sh', '-c', 'PYTHONPATH=. python3 tests/later.py']}]})
        base = validate(self.store, 'later', extraction['extraction_id'], config, sandbox=runner)
        future = validate(self.store, 'later', extraction['extraction_id'], config,
                          future_commit=self.later, sandbox=runner)
        self.assertEqual(base['interpretation'], 'inconclusive')
        self.assertEqual(future['interpretation'], 'passed')
        approval = session(self.store, 'later', stage='review', sandbox=runner,
            extraction_id=extraction['extraction_id'], command=command('review.json', report(self.review)))
        frozen = self.store.finalize('later', review_id=approval['review_id'])
        self.store.materialize('later', frozen['version'], self.root / 'live-public')
        self.assertFalse((self.root / 'live-public/tests').exists())
        self.assertEqual((self.root / 'live-public/app.py').read_text(), 'def answer(): return 0\n')
