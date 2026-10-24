"""Acceptance cases using genuine local history; no discovery or agent dependency."""
from contextlib import contextmanager
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from mineval.artifacts import digest, inventory, read_json, unpack, write_json
from mineval.preparation import Store
from mineval.sandbox import DockerSandbox, SandboxError, SandboxSpec
from mineval.service import broker, request
from mineval.verification import diagnose


class HistoryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / 'app.py').write_text('def answer(): return 0\n')
        (self.repo / '.gitattributes').write_text('app.py export-ignore\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        (self.repo / 'app.py').write_text('def answer(): return 42\n')
        (self.repo / 'tests').mkdir()
        (self.repo / 'tests/test_answer.py').write_text('from app import answer\nassert answer() == 42\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'solve and test')
        self.head = self.git('rev-parse', 'HEAD')
        self.store = Store(self.root / 'data')
        self.candidate = {'task_id': 'example', 'repository': 'https://example.org/project',
                          'base_commit': self.base, 'head_commit': self.head,
                          'task_summary': 'Return the correct answer',
                          'base_source': {'type': 'git', 'path': str(self.repo)}}
        self.store.register(self.candidate)
        self.workspace = self.store.draft('example') / 'workspace'
        self.config_path = self.store.draft('example') / 'control/verify.json'
        write_json(self.config_path, {'image': 'local:test', 'network': 'bridge', 'commands': [
            {'name': 'smoke', 'kind': 'smoke', 'argv': ['/bin/sh', '-c', 'test -f app.py']}]})
        diagnose(self.store, 'example', sandbox=FakeRunner([step()]))
        self.review = {'objective_preserved': True, 'snapshot_reviewed': True, 'reviewer': 'test reviewer', 'changes': []}

    def prepare_review(self, task_id, review):
        # Existing artifact tests now pass through separate fake producing/review sessions.
        from mineval.cleanup import save_attempt
        from mineval.review import session
        from mineval.sandbox import RunResult
        from review_helpers import ReportAgent, plan, report
        task = self.store.task(task_id)
        if task_id == 'example':
            with self.store.operation(task_id), tempfile.TemporaryDirectory(dir=self.store.root) as temp:
                from mineval.artifacts import copy_tree
                temp = Path(temp)
                copy_tree(self.store.draft(task_id) / 'workspace', temp / 'clean')
                control = temp / 'control'; control.mkdir()
                write_json(control / 'verify.json', read_json(self.store.draft(task_id) / 'control/verify.json'))
                write_json(control / 'cleanup-report.json', {
                    'disposition': 'ready-for-review', 'summary': 'Fixture cleanup',
                    'objective_preserved': review['objective_preserved'], 'changes': review['changes'],
                    'environment': {'runtime': 'fixture', 'dependencies': [], 'services': [], 'limitations': []}})
                if not (task / 'verify-latest.json').exists():
                    raise ValueError('Missing verify-latest diagnostic')
                diagnostic = read_json(task / 'verify-latest.json')
                save_attempt(self.store, task_id, temp / 'clean', control,
                    RunResult('fixture', 'image', 0, False, False, '', '', False, 0),
                    image_id='image', diagnostic_ids=[diagnostic['diagnostic_id']],
                    producer={'harness': 'fixture'}, input_hashes=inventory(temp / 'clean'),
                    instructions_sha256='fixture')
        metadata = read_json(task / 'metadata.json')
        sha = metadata['head_commit']
        path = 'check.py' if task_id == 'cleanup' else 'tests/test_answer.py'
        extraction = session(self.store, task_id, stage='extraction', command=['fixture-extractor'],
            sandbox=ReportAgent('selection.json', plan(sha, path)))
        approval = session(self.store, task_id, stage='review', command=['fixture-reviewer'],
            extraction_id=extraction['extraction_id'], sandbox=ReportAgent('review.json', report(review)))
        return approval['review_id']

    def freeze(self, task_id, review):
        return self.store.finalize(task_id, review_id=self.prepare_review(task_id, review))

    def git(self, *args):
        return subprocess.check_output(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.org',
                                        '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null',
                                        '-C', str(self.repo), *args], stderr=subprocess.DEVNULL).decode().strip()


class ArtifactAcceptanceTests(HistoryFixture):
    def test_import_has_exact_base_and_no_future_or_git(self):
        self.assertEqual((self.workspace / 'app.py').read_text(), 'def answer(): return 0\n')
        self.assertFalse((self.workspace / 'tests').exists())
        self.assertFalse((self.workspace / '.git').exists())
        self.assertTrue((self.store.task('example') / 'private/future' / self.head / 'tests/test_answer.py').exists())
        self.assertEqual(read_json(self.store.task('example') / 'metadata.json')['grounding'], 'git-objects-and-ancestry-checked')

    def test_freeze_materialize_independence_extraction_and_new_version(self):
        result = self.freeze('example', self.review)
        frozen = self.store.task('example') / 'versions' / result['version'] / 'snapshot.tar.gz'
        initial = digest(frozen)
        for name in ('one', 'two'):
            self.store.materialize('example', result['version'], self.root / name)
        (self.root / 'one/app.py').write_text('one')
        (self.root / 'two/app.py').write_text('two')
        self.assertEqual(digest(frozen), initial)
        extracted = self.store.extract('example', [{'commit': self.head, 'path': 'tests/test_answer.py',
                                                  'purpose': 'Correct answer regression', 'test_only': True}])
        self.assertEqual(digest(frozen), initial)
        manifest = self.store.task('example') / 'private/extractions' / extracted['extraction_id'] / 'extraction-manifest.json'
        self.assertFalse(read_json(manifest)['applied'])
        self.assertFalse((self.root / 'one/tests').exists())
        (self.workspace / 'environment.txt').write_text('setup adaptation')
        review = self.review | {'changes': [{'path': 'environment.txt', 'reason': 'Document offline setup'}]}
        diagnose(self.store, 'example', sandbox=FakeRunner([step(1)]))
        second = self.freeze('example', review)
        self.assertNotEqual(result['version'], second['version'])
        self.assertEqual(digest(frozen), initial)

    def test_changes_require_explicit_review_and_rejection_blocks_publish(self):
        (self.workspace / 'app.py').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'ready-for-review|every changed'):
            self.freeze('example', self.review)
        with self.assertRaisesRegex(ValueError, 'ready-for-review|integrity'):
            self.freeze('example', self.review | {'objective_preserved': False})
        self.store.reject('example', 'Cleanup would remove the target behavior')
        with self.assertRaisesRegex(ValueError, 'rejected'):
            self.freeze('example', self.review)
        self.assertFalse((self.store.task('example') / 'versions').exists())

    def test_hash_tamper_unknown_commit_and_production_attestation(self):
        selection = {'commit': self.head, 'path': 'tests/test_answer.py', 'purpose': 'Regression', 'test_only': True}
        for bad in (selection | {'commit': 'a' * 40}, selection | {'test_only': False}, selection | {'path': '../app.py'}):
            with self.assertRaises(ValueError):
                self.store.extract('example', [bad])
        source = self.store.task('example') / 'private/future' / self.head / 'tests/test_answer.py'
        original_source = source.read_bytes()
        source.write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'modified'):
            self.store.extract('example', [selection])
        source.write_bytes(original_source)
        result = self.freeze('example', self.review)
        frozen = self.store.task('example') / 'versions' / result['version'] / 'snapshot.tar.gz'
        frozen.chmod(0o644)
        frozen.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.store.materialize('example', result['version'], self.root / 'bad')

    def test_raw_boundary_accepts_tree_and_archive_and_keeps_unknown_notes_unknown(self):
        raw = self.root / 'raw'
        raw.mkdir()
        (raw / 'app.py').write_text('base')
        future = self.root / 'evidence'
        (future / self.head).mkdir(parents=True)
        (future / self.head / 'check.py').write_text('assert True')
        from mineval.artifacts import pack
        pack(raw, self.root / 'raw.tar.gz')
        candidate = self.candidate | {'task_id': 'raw', 'base_source': {'type': 'archive', 'path': str(self.root / 'raw.tar.gz')},
                                      'future_source': {'type': 'tree', 'path': str(future)},
                                      'source_attestation': 'Producer exported these SHAs and inspected future evidence'}
        self.store.register(candidate)
        metadata = read_json(self.store.task('raw') / 'metadata.json')
        self.assertEqual(metadata['grounding'], 'producer-attested-raw-export')
        self.assertNotIn('cleanup_risks', metadata)

    def test_unsafe_archive_and_workspace_links_rejected(self):
        for name, kind in (('../escape', tarfile.REGTYPE), ('link', tarfile.SYMTYPE), ('.git/config', tarfile.REGTYPE)):
            archive = self.root / 'bad.tar'
            with tarfile.open(archive, 'w') as tar:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = '/etc/passwd'
                tar.addfile(member, io.BytesIO(b''))
            with self.assertRaises(ValueError):
                unpack(archive, self.root / 'output')
            self.assertFalse((self.root / 'output').exists())
        (self.workspace / 'link').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError, 'Links'):
            self.freeze('example', self.review)

    def test_busy_task_prevents_parallel_finalize(self):
        with self.store.operation('example'):
            with self.assertRaisesRegex(ValueError, 'busy'):
                self.freeze('example', self.review)


class FakeRunner:
    def __init__(self, results):
        self.results = results
        self.specs = []
        self.destroyed = 0

    @contextmanager
    def container(self, spec):
        self.specs.append(spec)
        # Fresh copies can be written without changing the draft.
        (spec.workspace / 'diagnostic-only').write_text('scratch')
        container = Mock(id='fresh-' + str(len(self.specs)), image_id='sha256:image')
        container.exec.side_effect = self.results
        container.inspect.return_value = {'Running': True}
        try:
            yield container
        finally:
            self.destroyed += 1


def step(code=0, timeout=False):
    return {'exit_code': code, 'timed_out': timeout, 'oom_killed': False,
            'stdout': 'stdout', 'stderr': 'stderr', 'logs_truncated': False, 'duration_seconds': 0.01}


class DiagnosticsAcceptanceTests(HistoryFixture):
    def configure(self):
        config = read_json(self.config_path)
        config['commands'] = [{'name': name, 'kind': kind, 'argv': argv, 'timeout_seconds': 1}
                              for name, kind, argv in [('setup', 'setup', ['setup']), ('test', 'tests', ['tests']), ('missing', 'smoke', ['missing'])]]
        write_json(self.config_path, config)

    def test_repeated_fresh_reports_with_expected_failure_and_missing_command(self):
        self.configure()
        runner = FakeRunner([step(), step(1), step(127)])
        reports = [diagnose(self.store, 'example', sandbox=runner) for _ in range(2)]
        for report in reports:
            self.assertEqual(report['status'], 'environment_error')
            self.assertEqual([x['status'] for x in report['commands']], ['passed', 'failed', 'command_error'])
            self.assertEqual(report['commands'][1]['stderr'], 'stderr')
        self.assertNotEqual(reports[0]['container_id'], reports[1]['container_id'])
        self.assertNotEqual(runner.specs[0].workspace, runner.specs[1].workspace)
        self.assertEqual(runner.destroyed, 2)
        self.assertFalse((self.workspace / 'diagnostic-only').exists())
        self.assertEqual(read_json(self.store.task('example') / 'status.json')['status'], 'draft')

    def test_timeout_skips_later_steps_and_environment_failure_is_distinct(self):
        self.configure()
        report = diagnose(self.store, 'example', sandbox=FakeRunner([step(timeout=True)]))
        self.assertEqual([x['status'] for x in report['commands']], ['timeout', 'skipped', 'skipped'])
        runner = Mock()
        runner.container.side_effect = SandboxError('image unavailable')
        report = diagnose(self.store, 'example', sandbox=runner)
        self.assertEqual(report['status'], 'environment_error')
        self.assertIn('image unavailable', report['error'])

    def test_broker_scoped_protocol_pauses_and_resumes_agent(self):
        self.configure()
        runner, agent = FakeRunner([step(), step(1), step(127)]), Mock()
        with self.store.operation('example'), broker(self.store, 'example', runner, agent) as directory:
            report = request(socket_path=str(directory / 'verify.sock'))
            self.assertEqual(report['status'], 'environment_error')
            self.assertEqual(request('report', str(directory / 'verify.sock')), report)
            self.assertIn('error', request('mount-host', str(directory / 'verify.sock')))
        agent.pause.assert_called_once()
        agent.unpause.assert_called_once()

    def test_invalid_configuration_is_reported_without_container_creation(self):
        config = read_json(self.config_path)
        config['network'] = 'host'
        write_json(self.config_path, config)
        runner = Mock()
        report = diagnose(self.store, 'example', sandbox=runner)
        self.assertEqual(report['status'], 'environment_error')
        runner.container.assert_not_called()



class PublicationGuardTests(HistoryFixture):
    def test_stale_environment_only_and_missing_reports_cannot_publish(self):
        (self.workspace / 'setup.txt').write_text('change')
        review = self.review | {'changes': [{'path': 'setup.txt', 'reason': 'Environment setup note'}]}
        with self.assertRaisesRegex(ValueError, 'ready-for-review'):
            self.freeze('example', review)
        runner = Mock()
        runner.container.side_effect = SandboxError('cannot start')
        diagnose(self.store, 'example', sandbox=runner)
        with self.assertRaisesRegex(ValueError, 'ready-for-review'):
            self.freeze('example', review)
        (self.store.task('example') / 'verify-latest.json').unlink()
        with self.assertRaisesRegex(ValueError, 'verify-latest'):
            self.freeze('example', review)
        self.assertFalse((self.store.task('example') / 'versions').exists())

    def test_deterministic_archives_and_reserved_destination_rejected(self):
        first = self.freeze('example', self.review)
        second = self.freeze('example', self.review)
        self.assertEqual(first['snapshot_sha256'], second['snapshot_sha256'])
        with self.assertRaisesRegex(ValueError, 'protected'):
            self.store.materialize('example', first['version'], self.store.task('example') / 'bad')
        self.store.reject('example', 'Later review found objective interference')
        with self.assertRaisesRegex(ValueError, 'rejected'):
            self.store.materialize('example', first['version'], self.root / 'bad')

    @patch('mineval.service.DockerSandbox')
    def test_cleanup_session_mounts_no_private_history(self, runner_class):
        from mineval.service import session
        from mineval.sandbox import RunResult
        runner = runner_class.return_value
        container = Mock()
        container.wait.return_value = False
        container.inspect.return_value = {'Running': False, 'ExitCode': 0}
        container.image_id = 'sha256:image'
        container.result.return_value = RunResult('id', 'image', 0, False, False, '', '', False, 0)
        runner.container.return_value.__enter__.return_value = container
        session(self.store, 'example', ['generic-executable', 'arg'])
        spec = runner.container.call_args.args[0]
        self.assertNotEqual(spec.workspace, self.workspace.resolve())
        self.assertNotEqual(spec.control, self.config_path.parent.resolve())
        self.assertEqual(spec.command, ('generic-executable', 'arg'))
        self.assertIsNotNone(spec.broker)
        self.assertNotIn('private', str(spec.workspace))
        self.assertFalse(spec.broker.exists())
        self.assertFalse(spec.retain_logs)
        self.assertEqual(read_json(self.store.task('example') / 'cleanup-latest.json')['disposition'], 'needs-review')

    @patch('mineval.service.DockerSandbox')
    def test_extraction_session_separate_private_copy_only_saves_suggestions(self, runner_class):
        from mineval.service import session
        from mineval.sandbox import RunResult
        before = inventory(self.workspace)
        def run(spec):
            self.assertEqual((spec.workspace / 'base/app.py').read_text(), 'def answer(): return 0\n')
            self.assertEqual((spec.workspace / 'future' / self.head / 'app.py').read_text(), 'def answer(): return 42\n')
            self.assertIsNone(spec.broker)
            selection = [{'commit': self.head, 'path': 'tests/test_answer.py', 'purpose': 'regression', 'test_only': True}]
            write_json(spec.workspace / 'selection.json', selection)
            return RunResult('id', 'image', 0, False, False, '', '', False, 0)
        runner_class.return_value.run.side_effect = run
        session(self.store, 'example', ['generic-extractor'], stage='extraction')
        self.assertEqual(inventory(self.workspace), before)
        private = self.store.task('example') / 'private'
        self.assertTrue((private / 'extraction-selection.json').exists())
        self.assertFalse((private / 'extractions').exists())


if __name__ == "__main__":
    unittest.main()
