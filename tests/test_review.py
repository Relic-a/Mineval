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
        for result, interpretation in [(step(1), 'behavior_failure'), (step(127), 'environment_failure'),
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
        self.assertEqual(base['interpretation'], 'behavior_failure')
        self.assertEqual(future['interpretation'], 'passed')
        approval = session(self.store, 'later', stage='review', sandbox=runner,
            extraction_id=extraction['extraction_id'], command=command('review.json', report(self.review)))
        frozen = self.store.finalize('later', review_id=approval['review_id'])
        self.store.materialize('later', frozen['version'], self.root / 'live-public')
        self.assertFalse((self.root / 'live-public/tests').exists())
        self.assertEqual((self.root / 'live-public/app.py').read_text(), 'def answer(): return 0\n')
