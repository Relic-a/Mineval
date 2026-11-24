"""Deterministic selector -> CLI proof with a feature/fix/later-test history."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from mineval.core.artifacts import write_json
from mineval.cli import main
from mineval.preparation import Store


class DiscoveryIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'project'
        self.repo.mkdir()
        self.git('init', '-q')
        self.base = self.save('dispatch.py', 'def dispatch(value):\n    return value.upper()\n', 'Single request API')
        self.feature = self.save('dispatch.py', 'def dispatch(value):\n    if isinstance(value, list) and value:\n        return [dispatch(item) for item in value]\n    return value.upper()\n', 'Add batched requests')
        self.head = self.save('dispatch.py', 'def dispatch(value):\n    if isinstance(value, list):\n        return [dispatch(item) for item in value]\n    return value.upper()\n', 'Handle empty batch')
        (self.repo / 'tests').mkdir()
        self.evidence = self.save('tests/test_batch.py', 'import unittest\nfrom dispatch import dispatch\n\nclass BatchTests(unittest.TestCase):\n    def test_batch(self):\n        self.assertEqual(dispatch([]), [])\n        self.assertEqual(dispatch(["b", "a"]), ["B", "A"])\n        self.assertEqual(dispatch("a"), "A")\n', 'Regression coverage for batch boundaries')
        self.store = Store(self.root / 'private-data')
        self.candidate = {
            'schema_version': 2, 'category': 'integration',
            'repository': str(self.repo), 'base_source': {'type': 'git', 'path': 'project'},
            'base_commit': self.base, 'head_commit': self.head,
            'related_commits': [self.feature, self.head],
            'task_summary': 'Support batched requests, including empty batches, preserving order and single-request behavior.',
            'future_evidence': [{'commit': self.evidence, 'path': 'tests/test_batch.py',
                                 'proves': 'Empty batches, order and single requests work.'}],
            'execution': {'runtime': 'Python 3', 'likely_commands': ['python -m unittest discover -s tests'],
                          'cleanup_risks': [], 'external_requirements': []},
            'uncertainty': [], 'verification_feasibility': 'Behavioral tests use the existing public API.',
            'selection_status': 'accept'}

    def git(self, *args):
        env = os.environ | {'GIT_AUTHOR_DATE': '2020-01-01T00:00:00Z',
                            'GIT_COMMITTER_DATE': '2020-01-01T00:00:00Z'}
        return subprocess.check_output(['git', '-C', str(self.repo), '-c', 'user.name=Fixture',
                                       '-c', 'user.email=fixture@example.org', '-c', 'commit.gpgsign=false',
                                       '-c', 'core.hooksPath=/dev/null', *args], env=env,
                                      stderr=subprocess.DEVNULL).decode().strip()

    def save(self, path, content, message):
        (self.repo / path).write_text(content)
        self.git('add', '.')
        self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    def add(self, candidate):
        handoff = self.root / 'candidate.json'
        write_json(handoff, candidate)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--data', str(self.store.root), 'task', 'add', str(handoff)])
        return code, json.loads(out.getvalue()) if out.getvalue() else err.getvalue()

    def direct(self, **overrides):
        return self.candidate | {'base_source': {'type': 'git', 'path': str(self.repo)}} | overrides

    def test_new_schema_requires_primary_category_while_legacy_remains_importable(self):
        from mineval.historical.intake import normalize
        candidate = self.direct()
        for value in (None, '', 'feature', 'unknown', []):
            with self.subTest(category=value), self.assertRaisesRegex(ValueError, 'primary category'):
                normalize(candidate | {'category': value})
        candidate.pop('category')
        with self.assertRaisesRegex(ValueError, 'primary category'):
            normalize(candidate)
        candidate.pop('schema_version')
        normalized, _ = normalize(candidate)
        self.assertNotIn('category', normalized)

    def test_feature_fix_later_test_handoff_and_private_retention(self):
        # Prove the cited check distinguishes the feature's bug from the final state.
        check = (self.repo / 'tests/test_batch.py').read_bytes()
        for sha, expected in ((self.feature, 1), (self.head, 0)):
            state = self.root / sha
            state.mkdir()
            (state / 'dispatch.py').write_bytes(self.git('show', sha + ':dispatch.py').encode())
            (state / 'test_batch.py').write_bytes(check)
            result = subprocess.run(['python', '-m', 'unittest', 'test_batch'], cwd=state,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, expected)
        code, result = self.add(self.candidate)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['intake']['status'], 'complete')
        task_id = result['task_id']
        metadata = self.store.inspect_task(task_id)['metadata']
        self.assertEqual(metadata['base_commit'], self.base)
        self.assertEqual(metadata['head_commit'], self.head)
        self.assertEqual(metadata['future_commits'], [self.feature, self.head, self.evidence])
        workspace = self.store.draft(task_id) / 'workspace'
        self.assertEqual([p.name for p in workspace.iterdir()], ['dispatch.py'])
        self.assertEqual((workspace / 'dispatch.py').read_text(), 'def dispatch(value):\n    return value.upper()\n')
        for sha in (self.feature, self.head, self.evidence):
            self.assertTrue((self.store.task(task_id) / 'private/diffs' / (sha + '.patch')).is_file())
        code, error = self.add(self.candidate)
        self.assertEqual(code, 125)
        self.assertIn('already registered', error)
        shutil.rmtree(self.repo)
        from mineval.historical.repository import future_tree
        with future_tree(self.store.task(task_id), self.evidence) as tree:
            self.assertEqual((tree / 'tests/test_batch.py').read_bytes(), check)
        self.assertEqual(Store(self.store.root).inspect_task(task_id)['metadata'], metadata)

    def test_incomplete_deferred_and_rejected_records_do_not_create_workspaces(self):
        for selection, state in (('defer', 'needs-review'), ('reject', 'rejected')):
            candidate = {'task_id': selection, 'repository': 'local-unavailable',
                         'task_summary': 'Investigate a possible race.', 'selection_status': selection,
                         'uncertainty': ['Base and reproducible evidence not known yet.']}
            code, result = self.add(candidate)
            self.assertEqual(code, 0, result)
            self.assertEqual(result['status'], state)
            self.assertIn('base_commit', result['intake']['missing_fields'])
            self.assertFalse(self.store.draft(selection).exists())
            self.assertFalse((self.store.task(selection) / 'private').exists())
            self.assertEqual(self.store.inspect_task(selection)['metadata']['uncertainty'], candidate['uncertainty'])
            with self.assertRaises(ValueError):
                with self.store.operation(selection):
                    self.fail('Deferred/rejected candidate became usable')
        self.assertEqual(len(self.store.list_tasks()), 2)

    def test_missing_boundary_is_recorded_without_inventing_head(self):
        code, result = self.add({'repository': 'local', 'task_summary': 'Candidate without boundaries'})
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'needs-review')
        self.assertNotIn('head_commit', self.store.inspect_task(result['task_id'])['metadata'])

    def test_reproduction_evidence_and_identity_do_not_depend_on_optional_notes(self):
        candidate = self.candidate | {'future_evidence': [
            {'commit': self.evidence, 'proves': 'Empty batch is accepted.',
             'reproduction': 'Call dispatch([]) and assert the result is [].'}]}
        code, result = self.add(candidate)
        self.assertEqual(code, 0, result)
        code, error = self.add(candidate | {'uncertainty': ['Check portability later.']})
        self.assertEqual(code, 125)
        self.assertIn('already registered', error)
        metadata = self.store.inspect_task(result['task_id'])['metadata']
        self.assertEqual(metadata['future_evidence'], candidate['future_evidence'])

    def test_invalid_records_and_unrelated_history_leave_no_partial_import(self):
        self.git('checkout', '--orphan', 'unrelated')
        other = self.save('other.py', 'unrelated = True\n', 'Unrelated root')
        bad = [None, [], self.direct(base_commit='short'), self.direct(future_evidence={}),
               self.direct(verification_feasibility=True),
               self.direct(related_commits='abc'), self.direct(execution={'cleanup_risks': 'unknown'}),
               self.direct(head_commit=self.base), self.direct(head_commit=other),
               self.direct(future_evidence=[{'commit': self.evidence, 'path': '../secret', 'proves': 'x'}]),
               self.direct(future_evidence=[{'commit': self.evidence, 'path': 'missing.py', 'proves': 'x'}]),
               self.direct(future_evidence=[{'commit': self.evidence, 'path': 'tests/test_batch.py'}])]
        for candidate in bad:
            with self.subTest(candidate=candidate), self.assertRaises((ValueError, TypeError)):
                self.store.register(candidate)
        self.assertEqual(self.store.list_tasks(), [])
        self.assertEqual(list((self.store.root / 'drafts').glob('*')), [])


if __name__ == '__main__':
    unittest.main()
