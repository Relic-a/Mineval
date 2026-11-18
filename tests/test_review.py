"""Phase 5: later private regressions, independent verdicts and immutable approval."""
from contextlib import contextmanager
import json
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

    def behavioral_checks(self):
        choice = plan(self.head) | {'artifacts': [{'origin': 'generated', 'path': 'cases.json',
            'purpose': 'Answer behavior', 'test_only': True,
            'content': json.dumps({'cases': [{'input': '', 'expected': '42\n'}]})}]}
        extraction = self.extract(choice)['extraction_id']
        config = self.root / (extraction + '.json')
        write_json(config, {'image': 'local:test', 'network': 'none', 'grading_type': 'isolated-argv-v1',
            'commands': [{'name': 'private', 'kind': 'tests',
                          'argv': ['python3', '-c', 'from app import answer; print(answer())']}]})
        return extraction, config

    def test_validation_is_private_distinguishes_failures_and_invalidates_review(self):
        from test_evaluation import EvaluationRunner
        extraction, config = self.behavioral_checks()
        initial = self.review_extraction(extraction)
        before = inventory(self.workspace)
        latest = self.store.task('example') / 'verify-latest.json'
        diagnostic_hash = digest(latest)
        base = validate(self.store, 'example', extraction, config, sandbox=EvaluationRunner())
        future = validate(self.store, 'example', extraction, config, future_commit=self.head, sandbox=EvaluationRunner())
        self.assertEqual(base['interpretation'], 'behavior_failure')
        self.assertEqual(future['interpretation'], 'passed')
        self.assertEqual(inventory(self.workspace), before)
        self.assertEqual(digest(latest), diagnostic_hash)
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.store.finalize('example', review_id=initial['review_id'])
        approval = self.review_extraction(extraction)
        frozen = self.store.finalize('example', review_id=approval['review_id'])
        self.assertTrue((self.store.task('example') / 'versions' / frozen['version'] / 'private/validations').exists())

    def test_validation_executes_fixed_checks_without_evaluator_inputs(self):
        from test_evaluation import EvaluationRunner
        extraction, config = self.behavioral_checks()
        before = inventory(self.store.task('example') / 'private/extractions' / extraction)
        runner = EvaluationRunner()
        result = validate(self.store, 'example', extraction, config, sandbox=runner)
        self.assertEqual(result['baseline']['verdict'], 'fail')
        self.assertEqual(len(runner.specs), 1)
        self.assertIsNone(runner.specs[0].inputs)
        self.assertIsNone(runner.specs[0].control)
        self.assertEqual(inventory(self.store.task('example') / 'private/extractions' / extraction), before)
        evidence = self.store.task('example') / 'private/validations' / extraction / result['validation_id']
        self.assertFalse(read_json(evidence / 'baseline/selected-checks.json')[0]['adapted'])
        self.assertFalse((evidence / 'adapted').exists())
        checked(evidence)

    def test_validation_rejects_production_adaptation_and_preserves_failures(self):
        from test_evaluation import EvaluationRunner
        extraction, config = self.behavioral_checks()
        runner = EvaluationRunner()
        with self.assertRaisesRegex(ValueError, 'no longer supported'):
            validate(self.store, 'example', extraction, config, sandbox=runner, command=['assess'])
        self.assertEqual(runner.specs, [])
        for mode in ('timeout', 'command-error'):
            result = validate(self.store, 'example', extraction, config, sandbox=EvaluationRunner(mode=mode))
            self.assertEqual(result['interpretation'], 'environment_failure')

    def test_review_can_record_unexecutable_base_as_a_limitation(self):
        from test_evaluation import EvaluationRunner
        from mineval.review import validate_review
        extraction, config = self.behavioral_checks()
        base = validate(self.store, 'example', extraction, config, sandbox=EvaluationRunner(mode='command-error'))
        future = validate(self.store, 'example', extraction, config, future_commit=self.head, sandbox=EvaluationRunner())
        self.assertEqual(base['interpretation'], 'environment_failure')
        self.assertEqual(future['interpretation'], 'passed')
        result = self.review_extraction(extraction, reasons=['Base execution failed; reference checks ran.'])
        with self.assertRaisesRegex(ValueError, 'fail on the unsolved base'):
            self.store.finalize('example', review_id=result['review_id'])
        with self.assertRaisesRegex(ValueError, 'explicit integrity'):
            validate_review(report(self.review) | {'snapshot_reviewed': False})

    def test_validation_rejects_source_assessment_and_records_fixed_failure(self):
        from test_evaluation import EvaluationRunner
        extraction, config = self.behavioral_checks()
        runner = EvaluationRunner()
        with self.assertRaisesRegex(ValueError, 'no longer supported'):
            validate(self.store, 'example', extraction, config, command=['assessor'], sandbox=runner)
        result = validate(self.store, 'example', extraction, config, sandbox=runner)
        self.assertEqual(result['interpretation'], 'behavior_failure')
        self.assertNotIn('failure_basis', result['report'])
        self.assertNotIn('candidate_evidence', result['report'])

    def test_validation_executes_selected_fixture_on_base_and_future(self):
        from test_evaluation import EvaluationRunner
        extraction, config = self.behavioral_checks()
        base = validate(self.store, 'example', extraction, config, sandbox=EvaluationRunner())
        future = validate(self.store, 'example', extraction, config, future_commit=self.head, sandbox=EvaluationRunner())
        self.assertEqual(base['interpretation'], 'behavior_failure')
        self.assertEqual(future['interpretation'], 'passed')
        self.assertEqual(base['report']['configuration'], read_json(config))
        self.assertEqual(base['report']['selected_checks'][0]['path'], 'cases.json')
        self.assertFalse((self.workspace / 'cases.json').exists())

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
        def command(filename, value):
            return ['python3', '-c', 'import pathlib; pathlib.Path(' + repr('/mineval-control/' + filename) +
                    ').write_text(' + repr(json.dumps(value)) + ')']
        historical = session(self.store, 'later', stage='extraction', sandbox=runner,
            command=command('selection.json', plan(self.later, 'tests/later.py')))
        selected = self.store.task('later') / 'private/extractions' / historical['extraction_id']
        self.assertEqual(read_json(selected / 'extraction-manifest.json')['artifacts'][0]['commit'], self.later)
        generated = plan(self.later, 'tests/later.py') | {'artifacts': [{
            'origin': 'generated', 'path': 'cases.json', 'purpose': 'Behavior of the later regression',
            'test_only': True, 'content': json.dumps({'cases': [{'input': '', 'expected': '42\n'}]})}]}
        extraction = session(self.store, 'later', stage='extraction', sandbox=runner,
            command=command('selection.json', generated))
        config = self.root / 'private-live.json'
        write_json(config, {'image': os.environ['MINEVAL_TEST_IMAGE'], 'network': 'none',
            'grading_type': 'isolated-argv-v1', 'commands': [
                {'name': 'regression', 'kind': 'tests', 'argv': ['python3', '-c',
                    'from app import answer; print(answer())']}]})
        base = validate(self.store, 'later', extraction['extraction_id'], config, sandbox=runner)
        future = validate(self.store, 'later', extraction['extraction_id'], config,
                          future_commit=self.later, sandbox=runner)
        reference = validate(self.store, 'later', extraction['extraction_id'], config,
                             future_commit=self.head, sandbox=runner)
        self.assertEqual(base['interpretation'], 'behavior_failure')
        self.assertEqual(future['interpretation'], 'passed')
        self.assertEqual(reference['interpretation'], 'passed')
        approval = session(self.store, 'later', stage='review', sandbox=runner,
            extraction_id=extraction['extraction_id'], command=command('review.json', report(self.review)))
        frozen = self.store.finalize('later', review_id=approval['review_id'])
        self.store.materialize('later', frozen['version'], self.root / 'live-public')
        self.assertFalse((self.root / 'live-public/tests').exists())
        self.assertEqual((self.root / 'live-public/app.py').read_text(), 'def answer(): return 0\n')
