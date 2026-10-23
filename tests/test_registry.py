"""Registry and command boundaries; real local history, simulated diagnostics only."""
import contextlib
import io
import json
import subprocess
import sys
from unittest.mock import patch

from mineval.artifacts import digest, read_json, write_json
from mineval.cli import main
from mineval.preparation import Store
from test_preparation import HistoryFixture


class RegistryTests(HistoryFixture):
    def test_identity_survives_reopen_and_pool_growth(self):
        reopened = Store(self.store.root)
        self.assertEqual(reopened.list_tasks()[0]['task_id'], 'example')
        for directory in ('runs', 'evaluations'):
            self.assertTrue((reopened.task('example') / directory).is_dir())
        with self.assertRaisesRegex(ValueError, 'already registered'):
            reopened.register(self.candidate)
        reopened.register(self.candidate | {'task_id': 'another'})
        self.assertEqual([t['task_id'] for t in reopened.list_tasks()], ['another', 'example'])
        reopened.reject('another', 'Not suitable')
        self.assertEqual(reopened.inspect_task('another')['state']['status'], 'rejected')
        self.assertEqual(reopened.inspect_task('example')['runs'], [])
        # Old stores need no migration just to inspect empty record namespaces.
        for directory in ('runs', 'evaluations'):
            (reopened.task('example') / directory).rmdir()
        self.assertEqual(reopened.inspect_task('example')['evaluations'], [])

    def test_suite_is_pinned_across_new_revisions_and_tasks(self):
        first = self.store.finalize('example', self.review)['version']
        definition = {'suite_id': 'baseline-v1', 'description': 'Fixed comparison',
                      'members': [{'task_id': 'example', 'version': first}]}
        record = self.store.create_suite(definition)
        path = self.store.root / 'suites/baseline-v1/suite.json'
        initial = digest(path)
        self.store.register(self.candidate | {'task_id': 'new-task'})
        second = self.store.finalize('example', self.review)['version']
        self.assertNotEqual(first, second)
        self.assertEqual(Store(self.store.root).inspect_suite('baseline-v1'), record)
        self.assertEqual(digest(path), initial)
        self.assertEqual(record['members'][0], self.store.inspect_revision('example', first))
        self.assertEqual(self.store.inspect_task('example')['prepared_revisions'], sorted([first, second]))
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.store.create_suite(definition)
        with self.assertRaisesRegex(ValueError, 'protected'):
            self.store.materialize('example', first, self.store.root / 'suites/overwritten')
        self.store.reject('example', 'Later pool eligibility decision')
        self.assertEqual(self.store.inspect_suite('baseline-v1'), record)
        self.assertEqual(digest(path), initial)

    def test_invalid_members_cannot_publish_partial_suites(self):
        version = self.store.finalize('example', self.review)['version']
        member = {'task_id': 'example', 'version': version}
        for members in ([], [member, member], [{'task_id': 'example'}],
                        [member | {'version': 'latest'}], [member | {'task_id': 'missing'}],
                        [member | {'task_id': '../escape'}]):
            with self.subTest(members=members), self.assertRaises(ValueError):
                self.store.create_suite({'suite_id': 'invalid', 'members': members})
            self.assertFalse((self.store.root / 'suites/invalid').exists())
        self.store.reject('example', 'Rejected tasks cannot enter new suites')
        with self.assertRaisesRegex(ValueError, 'rejected'):
            self.store.create_suite({'suite_id': 'invalid', 'members': [member]})

    def test_revision_and_suite_tampering_is_detected(self):
        version = self.store.finalize('example', self.review)['version']
        definition = {'suite_id': 'baseline', 'members': [{'task_id': 'example', 'version': version}]}
        self.store.create_suite(definition)
        frozen = self.store.task('example') / 'versions' / version
        config = frozen / 'verify-config.json'
        config.chmod(0o644)
        write_json(config, {'changed': True})
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.store.inspect_revision('example', version)
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.store.create_suite(definition | {'suite_id': 'bad'})
        path = self.store.root / 'suites/baseline/suite.json'
        path.chmod(0o644)
        write_json(path, {'members': []})
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.store.inspect_suite('baseline')

    def test_cli_roundtrip_across_independent_processes(self):
        candidate = self.root / 'candidate.json'
        write_json(candidate, self.candidate | {'task_id': 'cli-task'})
        version = self.store.finalize('example', self.review)['version']
        definition = self.root / 'suite.json'
        write_json(definition, {'suite_id': 'cli-suite', 'members': [{'task_id': 'example', 'version': version}]})
        for command in (['task', 'add', str(candidate)], ['task', 'list'],
                        ['task', 'show', 'cli-task'], ['task', 'revision', 'example', version],
                        ['suite', 'create', str(definition)], ['suite', 'show', 'cli-suite']):
            result = subprocess.run([sys.executable, '-m', 'mineval', '--data', str(self.store.root), *command],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout))
        self.assertEqual(read_json(self.store.task('cli-task') / 'metadata.json')['task_id'], 'cli-task')

    @patch('mineval.cli.DockerSandbox')
    def test_unimplemented_operations_fail_without_store_or_docker(self, sandbox):
        absent = self.root / 'absent'
        for command in (['bench', 'run'], ['bench', 'run', 'baseline'], ['evaluate', 'run-001']):
            with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out:
                code = main(['--data', str(absent), *command])
            self.assertEqual(code, 2)
            self.assertIn('not implemented', err.getvalue())
            self.assertEqual(out.getvalue(), '')
            self.assertFalse(absent.exists())
        sandbox.assert_not_called()

    def test_prepare_without_executable_fails_clearly(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(['--data', str(self.store.root), 'prepare', 'example'])
        self.assertEqual(code, 125)
        self.assertIn('explicit executable argv', err.getvalue())
