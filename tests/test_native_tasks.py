"""End-to-end native task preparation and independent saved attempts."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from mineval.historical import benchmark, evaluation, review
from mineval.core.artifacts import copy_tree, inventory, read_json, write_json
from mineval.historical.cleanup import save_attempt
from mineval.historical.native_grading import script_artifact
from mineval.core.sandbox import DockerSandbox, RunResult, SandboxSpec
from mineval.historical.verification import diagnose, load_config
from review_helpers import ReportAgent, report
from test_agents import SimulatedDocker
from test_preparation import HistoryFixture


CONTEXT = os.environ.get('MINEVAL_TEST_CONTEXT')
NODE_IMAGE = os.environ.get('MINEVAL_NATIVE_TEST_IMAGE')
POSTGRES_IMAGE = os.environ.get('MINEVAL_POSTGRES_TEST_IMAGE')
LOCAL_IMAGE = 'sha256:' + '1' * 64


class LocalNode:
    """Test double for Docker's mount paths; runs the real Node test runner."""
    def __init__(self):
        self.specs = []

    @contextmanager
    def container(self, spec):
        self.specs.append(spec)
        container = Mock(id='native-test', image_id=LOCAL_IMAGE)
        container.inspect.return_value = {'Running': True}

        def execute(argv, *, timeout, workdir):
            def map_path(path):
                return path.replace('/workspace', str(spec.workspace), 1)
            env = os.environ | {
                key: value.replace('/mineval-control', str(spec.control))
                for key, value in spec.environment.items()}
            result = subprocess.run([map_path(arg) for arg in argv], cwd=map_path(workdir),
                                    env=env, timeout=timeout, capture_output=True, text=True)
            return {'exit_code': result.returncode, 'timed_out': False, 'oom_killed': False,
                    'stdout': result.stdout, 'stderr': result.stderr, 'logs_truncated': False}
        container.exec.side_effect = execute
        yield container


class NativeTaskMixin:
    def native_task(self, runner, image):
        self.git('checkout', '--detach', self.base)
        project = self.repo / 'project'
        project.mkdir()
        (project / 'answer.cjs').write_text('exports.answer = () => 0;\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'library base')
        base = self.git('rev-parse', 'HEAD')
        (project / 'answer.cjs').write_text('exports.answer = () => 42;\n')
        (project / 'held-out.cjs').write_text("""
const test = require('node:test');
const assert = require('node:assert/strict');
const {answer} = require('./answer.cjs');
assert.ok(require('node:fs').existsSync('../.git/HEAD'));
test('public answer', () => assert.equal(answer(), 42));
test('skip is visible', {skip: true}, () => {});
""")
        (project / 'verify.sh').write_text(
            '#!/bin/sh\nexec node --test --test-reporter=junit held-out.cjs > "$MINEVAL_TEST_REPORT"\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'correct library and direct-import test')
        head = self.git('rev-parse', 'HEAD')
        brief = '# Library task\n\nExpose answer() as a named CommonJS export. It takes no arguments and returns the number 42. Keep the module path answer.cjs.\n'
        self.store.register(self.candidate | {'task_id': 'native', 'base_commit': base,
                            'head_commit': head, 'task_instructions': brief,
                            'schema_version': 2, 'category': 'behavior'})
        draft = self.store.draft('native')
        runtime = {'image': image, 'workdir': '/workspace/project', 'network': 'none',
                   'commands': [{'kind': 'smoke', 'name': 'load library',
                                 'argv': ['node', '-e', "require('./answer.cjs')"]}]}
        write_json(draft / 'control/verify.json', runtime)
        diagnostic = diagnose(self.store, 'native', sandbox=runner)
        self.assertEqual(diagnostic['status'], 'completed', diagnostic)
        config = {k: v for k, v in runtime.items() if k != 'commands'} | {
            'grading_type': 'native-script-v1', 'script': 'project/verify.sh',
            'required_tests': ['test::public answer']}
        cfg = self.root / 'native.json'
        write_json(cfg, config)
        task = self.store.task('native')
        with self.store.operation('native'), tempfile.TemporaryDirectory(dir=self.store.root) as temp:
            temp = Path(temp)
            clean, control = temp / 'clean', temp / 'control'
            copy_tree(draft / 'workspace', clean)
            write_json(control / 'verify.json', runtime)
            write_json(control / 'cleanup-report.json', {
                'disposition': 'ready-for-review', 'summary': 'No changes', 'objective_preserved': True,
                'changes': [], 'environment': {'runtime': 'Node', 'dependencies': [],
                                               'services': [], 'limitations': []}})
            image_id = diagnostic['image_id']
            save_attempt(self.store, 'native', clean, control,
                         RunResult('native', image_id, 0, False, False, '', '', False, 0),
                         image_id=image_id, diagnostic_ids=[diagnostic['diagnostic_id']],
                         producer={'harness': 'fixture'}, input_hashes=inventory(clean),
                         instructions_sha256='fixture')
        selection = {'strategy': 'focused', 'rationale': 'Exercise the public library export',
                     'dependencies': [], 'instructions': 'Use the repository test runner',
                     'limitations': [], 'evaluation_config': config, 'artifacts': [
                         {'commit': head, 'path': path, 'purpose': 'Direct library verification', 'test_only': True}
                         for path in ('project/verify.sh', 'project/held-out.cjs')]}
        extraction = review.session(self.store, 'native', stage='extraction', command=['fixture-extractor'],
                                    sandbox=ReportAgent('selection.json', selection))['extraction_id']
        for commit, expected in ((None, 'fail'), (head, 'pass')):
            validation = review.validate(self.store, 'native', extraction, cfg,
                                         future_commit=commit, sandbox=runner)
            self.assertEqual(validation['report']['verdict'], expected, validation)
            self.assertEqual(validation['report']['image_id'], image_id)
        approval = review.session(self.store, 'native', stage='review', command=['fixture-reviewer'],
                                  extraction_id=extraction,
                                  sandbox=ReportAgent('review.json', report(self.review)))['review_id']
        (draft / 'control/task-brief.md').write_text(brief + 'Changed after review.')
        with self.assertRaisesRegex(ValueError, 'review is stale'):
            self.store.finalize('native', review_id=approval)
        (draft / 'control/task-brief.md').write_text(brief)
        version = self.store.finalize('native', review_id=approval)['version']
        (draft / 'control/task-brief.md').write_text('Changed after freezing.')
        frozen = task / 'versions' / version
        self.assertEqual((frozen / 'task-brief.md').read_text(), brief)

        test = self
        class CodingAttempt(SimulatedDocker):
            @contextmanager
            def container(self, spec):
                test.assertEqual(spec.image, image_id)
                test.assertEqual(spec.workdir, '/workspace/project')
                files = inventory(spec.workspace, allow_git=True)
                test.assertFalse({'project/verify.sh', 'project/held-out.cjs'} & files.keys())
                test.assertIn(brief, (spec.inputs / 'prompt.txt').read_text())
                test.assertEqual((spec.workspace / 'project/answer.cjs').read_text(), 'exports.answer = () => 0;\n')
                with super().container(spec) as container:
                    original = container.start.side_effect
                    def start():
                        original()
                        (spec.workspace / 'project/answer.cjs').write_text(f'exports.answer = () => {self.value};\n')
                    container.start.side_effect = start
                    yield container

        for name, value, verdict in [('correct', 42, 'pass'), ('incorrect', 41, 'fail')]:
            coding = CodingAttempt()
            coding.value = value
            result = benchmark.run(self.store, 'native', version, run_id=name, sandbox=coding)
            self.assertEqual(result['status'], 'completed', result)
            assessed = evaluation.evaluate(self.store, 'native', name, cfg, evaluation_id=name, sandbox=runner)
            self.assertEqual(assessed['verdict'], verdict, assessed)
            self.assertEqual(assessed['tests']['skipped'], ['test::skip is visible'])
            self.assertEqual(assessed['tests']['failed'], [] if value == 42 else ['test::public answer'])

        from mineval.historical.native_grading import execute
        verification = frozen / 'private/verification'
        files = read_json(verification / 'extraction-manifest.json')['artifacts']
        copy = self.root / 'candidate'
        benchmark.export(self.store, 'native', 'correct', copy)
        normalized = load_config(cfg, evaluation=True)
        from mineval.core.history import restore
        def attach_base(workspace):
            restore(frozen / 'history.pack', base, workspace)
        missing = execute(copy, self.root / 'missing-workspace', self.root / 'missing-evidence', verification,
                          files, normalized | {'required_tests': ['test::missing test']}, image_id, runner,
                          prepare_workspace=attach_base)
        self.assertEqual(missing['commands'][0]['exit_code'], 0)
        self.assertEqual(missing['verdict'], 'fail')
        self.assertEqual(missing['tests']['missing'], ['test::missing test'])
        skipped = execute(copy, self.root / 'skip-workspace', self.root / 'skip-evidence', verification,
                          files, normalized | {'required_tests': ['test::skip is visible']}, image_id, runner,
                          prepare_workspace=attach_base)
        self.assertEqual(skipped['verdict'], 'fail')
        self.assertEqual(skipped['tests']['missing'], [])
        with self.assertRaisesRegex(ValueError, 'one frozen regular file'):
            script_artifact(files, config | {'script': 'project/directory.sh'})
        (copy / 'project/verify.sh').mkdir()
        rejected = execute(copy, self.root / 'rejected-workspace', self.root / 'rejected-evidence',
                           verification, files, normalized, image_id, runner)
        self.assertEqual(rejected['verdict'], 'error')
        self.assertIn('regular file', rejected['detail'])


class NativeTaskTests(NativeTaskMixin, HistoryFixture):
    @unittest.skipUnless(shutil.which('node'), 'Node built-in test runner required')
    def test_pipeline_direct_library_import(self):
        self.native_task(LocalNode(), LOCAL_IMAGE)


@unittest.skipUnless(NODE_IMAGE, 'set MINEVAL_NATIVE_TEST_IMAGE for live rootless verification')
class NativeDockerTests(NativeTaskMixin, HistoryFixture):
    def test_pipeline_direct_library_import_in_docker(self):
        self.native_task(DockerSandbox(context=CONTEXT), NODE_IMAGE)


@unittest.skipUnless(NODE_IMAGE and POSTGRES_IMAGE, 'set native and PostgreSQL images for service checks')
class LiveServices(unittest.TestCase):
    def test_database_dns_fresh_state_and_cleanup(self):
        runner = DockerSandbox(context=CONTEXT)
        image = runner.image(POSTGRES_IMAGE)['Id']
        service = {'name': 'database', 'image': image,
                   'environment': {'POSTGRES_PASSWORD': 'fixture-password'},
                   'tmpfs': ['/var/run/postgresql'],
                   'ready': ['pg_isready', '-h', '127.0.0.1'],
                   'limits': {'timeout_seconds': 30}}
        # A project process only needs a database dependency and its Docker DNS alias.
        probe = "const s=require('node:net').connect(5432,'database');s.on('connect',()=>s.end());"
        spec = SandboxSpec(NODE_IMAGE, ('node', '-e', probe), services=(service,))
        networks = []
        original = runner.container
        with original(spec) as project:
            network = runner._cli.json('inspect', project.id)[0]['HostConfig']['NetworkMode']
            networks.append(network)
            project.start()
            self.assertFalse(project.wait(10))
            self.assertEqual(project.inspect()['ExitCode'], 0)
            args = ['exec', network + '-database', 'psql', '-U', 'postgres', '-Atc']
            runner._cli.call(*args, 'create table attempt_state (id int)')
        with original(spec) as project:
            network = runner._cli.json('inspect', project.id)[0]['HostConfig']['NetworkMode']
            networks.append(network)
            out, _, _ = runner._cli.call('exec', network + '-database', 'psql', '-U', 'postgres', '-Atc',
                                        "select to_regclass('public.attempt_state') is null")
            self.assertEqual(out.strip(), 't')
        self.assertEqual(len(set(networks)), 2)
        for network in networks:
            with self.assertRaisesRegex(RuntimeError, 'not found|No such'):
                runner._cli.call('network', 'inspect', network)
