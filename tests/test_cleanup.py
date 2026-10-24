"""Cleanup preserves a real bug while repairing a historical setup prerequisite."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock, patch
import unittest

from mineval.artifacts import inventory, read_json, write_json
from mineval.service import request, session
from mineval.verification import diagnose
from test_preparation import HistoryFixture, FakeRunner, step


def producer_report():
    return {'disposition': 'ready-for-review', 'summary': 'Repair only the runtime selection',
            'objective_preserved': True,
            'changes': [{'path': 'runtime.txt', 'reason': 'Select the runtime supplied by the pinned image'}],
            'environment': {'runtime': 'Python 3.11 from the recorded local image ID',
                            'dependencies': [], 'services': [], 'limitations': []}}


class CleanupTests(HistoryFixture):
    def setUp(self):
        super().setUp()
        # Add the broken prerequisite and real failing test to the actual historical base.
        self.git('checkout', '-q', self.base)
        (self.repo / 'runtime.txt').write_text('unavailable\n')
        (self.repo / 'check.py').write_text('from app import answer\nassert answer() == 42\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'broken setup and task regression')
        self.cleanup_base = self.git('rev-parse', 'HEAD')
        (self.repo / 'app.py').write_text('def answer(): return 42\n')
        self.git('commit', '-qam', 'future solution')
        self.cleanup_head = self.git('rev-parse', 'HEAD')
        self.store.register(self.candidate | {'task_id': 'cleanup', 'base_commit': self.cleanup_base,
                                            'head_commit': self.cleanup_head})
        self.workspace = self.store.draft('cleanup') / 'workspace'
        self.config_path = self.store.draft('cleanup') / 'control/verify.json'
        write_json(self.config_path, {'image': os.environ.get('MINEVAL_TEST_IMAGE', 'local:test'),
                                     'limits': {'timeout_seconds': 60}, 'commands': [
            {'name': 'runtime', 'kind': 'setup', 'argv': ['python3', '-c',
                "from pathlib import Path; assert Path('runtime.txt').read_text().strip() == '3.11'"]},
            {'name': 'regression', 'kind': 'tests', 'argv': ['python3', 'check.py'],
             'expected_failure': 'The historical answer bug is deliberately unfixed'}]})

    def run_producer(self, report=None, *, code=0):
        runner = FakeRunner([step(), step(1)])
        diagnostics = []
        @contextmanager
        def container(spec):
            if not spec.broker:
                with FakeRunner.container(runner, spec) as fresh:
                    yield fresh
                return
            self.assertNotEqual(spec.workspace, self.workspace)
            self.assertFalse(spec.retain_logs)
            self.assertEqual(subprocess.check_output(['git', '-C', str(spec.workspace), 'rev-parse', 'HEAD']).decode().strip(), self.cleanup_base)
            self.assertNotEqual(subprocess.run(['git', '-C', str(spec.workspace), 'cat-file', '-e', self.cleanup_head],
                                               stderr=subprocess.DEVNULL).returncode, 0)
            agent = Mock(id='cleanup-container', image_id='sha256:fixture')
            def start():
                diagnostics.append(request(socket_path=str(spec.broker / 'verify.sock')))
                (spec.workspace / 'runtime.txt').write_text('3.11\n')
                if report is not None:
                    write_json(spec.control / 'cleanup-report.json', report)
                diagnostics.append(request(socket_path=str(spec.broker / 'verify.sock')))
                # Agent Git is discarded; it is never used to reconstruct the draft.
                (spec.workspace / '.git/config').write_text('[include]\npath=/host-secret\n')
            agent.start.side_effect = start
            agent.wait.return_value = False
            agent.inspect.return_value = {'Running': False, 'ExitCode': code}
            yield agent
        runner.container = container
        with patch('mineval.service.DockerSandbox', return_value=runner):
            session(self.store, 'cleanup', ['fake-cleaner'])
        return read_json(self.store.task('cleanup') / 'cleanup-latest.json'), diagnostics

    def test_cleanup_repairs_environment_preserves_bug_and_saves_review_boundary(self):
        original = inventory(self.repo, allow_git=True)
        report, diagnostics = self.run_producer(producer_report())
        self.assertEqual(report['disposition'], 'ready-for-review')
        self.assertEqual(report['approval'], 'pending-independent-review')
        self.assertEqual(diagnostics[-1]['commands'][1]['interpretation'], 'expected_task_failure')
        self.assertEqual(report['changed_paths'], ['runtime.txt'])
        self.assertEqual((self.workspace / 'app.py').read_text(), 'def answer(): return 0\n')
        self.assertFalse((self.workspace / '.git').exists())
        self.assertFalse((self.workspace / 'diagnostic-only').exists())
        self.assertEqual(inventory(self.repo, allow_git=True), original)
        attempt = self.store.task('cleanup') / 'cleanups' / report['cleanup_id']
        self.assertTrue((attempt / 'workspace.tar.gz').is_file())
        self.assertIn('runtime.txt', (attempt / 'cleanup-changes.patch').read_text())
        review = self.review | {'changes': producer_report()['changes']}
        frozen = self.freeze('cleanup', review)
        self.assertTrue((self.store.task('cleanup') / 'versions' / frozen['version'] / 'cleanup-attempt.json').exists())
        restored = self.root / 'cleaned-historical-workspace'
        self.store.materialize('cleanup', frozen['version'], restored)
        self.assertEqual((restored / 'runtime.txt').read_text(), '3.11\n')
        self.assertEqual((restored / 'app.py').read_text(), 'def answer(): return 0\n')
        self.assertEqual(subprocess.check_output(['git', '-C', str(restored), 'rev-parse', 'HEAD']).decode().strip(), self.cleanup_base)
        self.assertNotEqual(subprocess.run(['git', '-C', str(restored), 'cat-file', '-e', self.cleanup_head],
                                           stderr=subprocess.DEVNULL).returncode, 0)
        (self.workspace / 'runtime.txt').write_text('stale')
        with self.assertRaisesRegex(ValueError, 'ready-for-review'):
            self.freeze('cleanup', review)

    def test_missing_reason_and_process_failure_require_review(self):
        report, _ = self.run_producer(producer_report() | {'changes': []}, code=7)
        self.assertEqual(report['disposition'], 'needs-review')
        self.assertTrue(any('every changed path' in issue for issue in report['issues']))
        self.assertTrue(any('successfully' in issue for issue in report['issues']))
        with self.assertRaisesRegex(ValueError, 'ready-for-review'):
            self.freeze('cleanup', self.review | {'changes': producer_report()['changes']})

    def test_task_altering_or_unavailable_setup_cannot_be_ready(self):
        from mineval.cleanup import validate_report
        with self.assertRaisesRegex(ValueError, 'Task-altering'):
            validate_report(producer_report() | {'objective_preserved': False}, ['runtime.txt'])
        rejected, _ = self.run_producer(producer_report() | {
            'disposition': 'reject', 'objective_preserved': False,
            'summary': 'Essential service would require unsafe production access'})
        self.assertEqual(rejected['disposition'], 'reject')
        self.assertEqual(self.store.inspect_task('cleanup')['cleanup']['disposition'], 'reject')
        with self.assertRaisesRegex(ValueError, 'ready-for-review'):
            self.freeze('cleanup', self.review | {'changes': producer_report()['changes']})

    def test_setup_failure_skips_tests_but_expected_bug_does_not(self):
        broken = diagnose(self.store, 'cleanup', sandbox=FakeRunner([step(1)]))
        self.assertEqual(broken['status'], 'environment_error')
        self.assertEqual([x['status'] for x in broken['commands']], ['failed', 'skipped'])
        (self.workspace / 'runtime.txt').write_text('3.11\n')
        repaired = diagnose(self.store, 'cleanup', sandbox=FakeRunner([step(), step(1)]))
        self.assertEqual(repaired['status'], 'checks_failed')
        self.assertEqual(repaired['commands'][1]['interpretation'], 'expected_task_failure')
        # Annotation is not inferred: unexplained nonzero tests stay behavior failures.
        config = read_json(self.config_path)
        del config['commands'][1]['expected_failure']
        write_json(self.config_path, config)
        unclassified = diagnose(self.store, 'cleanup', sandbox=FakeRunner([step(), step(1)]))
        self.assertEqual(unclassified['commands'][1]['interpretation'], 'behavior_failure')

    @unittest.skipUnless(os.environ.get('MINEVAL_TEST_IMAGE'), 'set MINEVAL_TEST_IMAGE for live cleanup checks')
    def test_live_dedicated_fake_cleanup_adapter(self):
        from mineval.agents import FakeAdapter
        config_path = self.store.draft('example') / 'control/verify.json'
        config = read_json(config_path)
        config['image'] = os.environ['MINEVAL_TEST_IMAGE']
        write_json(config_path, config)
        result = session(self.store, 'example', adapter=FakeAdapter(cleanup=True),
                         context=os.environ.get('MINEVAL_TEST_CONTEXT'))
        record = self.store.inspect_task('example')['cleanup']
        self.assertEqual(result.exit_code, 0, record)
        self.assertEqual(record['disposition'], 'ready-for-review', record)
        self.assertEqual(record['producer']['harness'], 'fake')
        self.assertEqual(record['changed_paths'], [])

    @unittest.skipUnless(os.environ.get('MINEVAL_TEST_IMAGE'), 'set MINEVAL_TEST_IMAGE for live cleanup checks')
    def test_live_cleanup_and_broker_with_real_environment_repair(self):
        import json
        script = '''
import json, pathlib, subprocess
from mineval.service import request
assert subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode().strip() == BASE
assert subprocess.run(['git', 'cat-file', '-e', FUTURE], stderr=subprocess.DEVNULL).returncode != 0
assert not pathlib.Path('/var/run/docker.sock').exists()
first = request()
assert first['status'] == 'environment_error', first
assert first['commands'][1]['status'] == 'skipped', first
pathlib.Path('runtime.txt').write_text('3.11\\n')
second = request()
assert first['container_id'] != second['container_id']
assert second['status'] == 'checks_failed', second
assert [c['interpretation'] for c in second['commands']] == ['passed', 'expected_task_failure'], second
assert pathlib.Path('app.py').read_text() == 'def answer(): return 0\\n'
pathlib.Path('/mineval-control/cleanup-report.json').write_text(json.dumps(REPORT))
'''
        script = 'BASE=' + repr(self.cleanup_base) + '\nFUTURE=' + repr(self.cleanup_head) + '\nREPORT=json.loads(' + repr(json.dumps(producer_report())) + ')\n' + script
        script = 'import json\n' + script
        result = session(self.store, 'cleanup', ['python3', '-c', script],
                         context=os.environ.get('MINEVAL_TEST_CONTEXT'))
        record = read_json(self.store.task('cleanup') / 'cleanup-latest.json')
        self.assertEqual(result.exit_code, 0, record)
        self.assertEqual(record['disposition'], 'ready-for-review', record)
        self.assertEqual((self.workspace / 'app.py').read_text(), 'def answer(): return 0\n')
        self.assertEqual(record['changed_paths'], ['runtime.txt'])
        self.assertFalse((self.workspace / '.git').exists())


if __name__ == '__main__':
    unittest.main()
