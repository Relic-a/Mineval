"""Focused Phase 3 fixtures: real local Git; simulated Docker; zero model calls."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import Mock

from mineval.agents import CodexAdapter, FakeAdapter, provision_auth, run_agent
from mineval.artifacts import inventory, read_json, unpack, write_json
from mineval.history import create_pack, git, objects, restore, validate
from mineval.sandbox import DockerSandbox, Limits
from test_preparation import HistoryFixture
from test_sandbox import READY, IMAGE_ID, CONTAINER_ID


class SimulatedDocker:
    """Only execute our fixed fake fixture, with container paths mapped to temp dirs."""
    def __init__(self, *, interrupt=False, bad_output=False):
        self.spec = None
        self.removed = False
        self.interrupt = interrupt
        self.bad_output = bad_output

    @contextmanager
    def container(self, spec):
        self.spec = spec
        container = Mock(image_id='sha256:fixture')
        def start():
            if self.interrupt:
                raise KeyboardInterrupt()
            if spec.command[0] != 'python3':
                raise AssertionError('Simulation never runs Codex')
            script = spec.command[2]
            for mount, path in (('/workspace', spec.workspace), ('/mineval-input', spec.inputs),
                                ('/mineval-control', spec.control)):
                script = script.replace(mount, str(path))
            if 'time.sleep(86400)' in script:
                script = script.replace('time.sleep(86400)', 'raise SystemExit(137)')
            proc = subprocess.run([sys.executable, '-c', script], capture_output=True, timeout=5)
            self.code = proc.returncode
            if self.bad_output:
                (spec.workspace / 'escape').symlink_to('/etc/passwd')
        container.start.side_effect = start
        container.wait.side_effect = lambda timeout: read_json(spec.inputs / 'fake.json')['outcome'] == 'timeout'
        container.inspect.side_effect = lambda: {'Running': False, 'ExitCode': self.code, 'OOMKilled': False}
        try:
            yield container
        finally:
            self.removed = True


class PhaseThreeTests(HistoryFixture):
    def materialized(self):
        version = self.store.finalize('example', self.review)['version']
        target = self.root / 'historical'
        self.store.materialize('example', version, target)
        return target

    def test_history_closure_preserves_merge_blame_and_excludes_every_future_object(self):
        self.git('checkout', '-qb', 'side', self.base)
        (self.repo / 'side.txt').write_text('old side')
        self.git('add', '.'); self.git('commit', '-qm', 'old side')
        side = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-qb', 'past-main', self.base)
        (self.repo / 'past.txt').write_text('old main')
        self.git('add', '.'); self.git('commit', '-qm', 'old main')
        self.git('merge', '--no-ff', '-qm', 'past merge', 'side')
        base = self.git('rev-parse', 'HEAD')
        self.git('tag', '-a', 'future-tag', self.head, '-m', 'private future tag')
        secret = subprocess.check_output(['git', '-C', str(self.repo), 'hash-object', '-w', '--stdin'],
                                         input=b'UNREACHABLE FUTURE SECRET').decode().strip()
        self.git('pack-refs', '--all')
        self.git('config', 'remote.origin.url', 'https://secret-token@example.org/future')
        self.git('config', 'alias.leak', '!echo future')
        archive = self.root / 'historical.pack'
        create_pack(self.repo, base, archive)
        restored = self.root / 'restored'
        restore(archive, base, restored, checkout=True)
        validate(restored, base)
        self.assertIn(side, git(restored, 'log', '--format=%H').decode())
        self.assertIn('old side', git(restored, 'blame', 'side.txt').decode())
        self.assertEqual(objects(restored, base), objects(self.repo, base))
        for sha in (self.head, secret):
            with self.assertRaises(ValueError):
                git(restored, 'cat-file', '-p', sha)
        self.assertNotIn('secret-token', (restored / '.git/config').read_text())
        self.assertEqual(git(restored, 'remote').strip(), b'')
        self.assertEqual(git(restored, 'reflog', '--all').strip(), b'')
        self.assertNotIn('tests/test_answer.py', inventory(restored, allow_git=True))
        git(restored, 'hash-object', '-w', '--stdin', input=b'unreachable extra object')
        with self.assertRaisesRegex(ValueError, 'outside'):
            validate(restored, base)
        # A pack containing no requested commit cannot be used.

        with self.assertRaises(ValueError):
            restore(self.store.task('example') / 'private/history.pack', self.head, self.root / 'invalid')

    def test_history_retained_after_source_deleted_and_preparation_edits_preserved(self):
        shutil.rmtree(self.repo)
        (self.workspace / 'setup.txt').write_text('setup only')
        from mineval.verification import diagnose
        from test_preparation import FakeRunner, step
        diagnose(self.store, 'example', sandbox=FakeRunner([step()]))
        self.review['changes'] = [{'path': 'setup.txt', 'reason': 'fixture environment'}]
        target = self.materialized()
        self.assertEqual(git(target, 'rev-parse', 'HEAD').decode().strip(), self.base)
        self.assertIn('setup.txt', git(target, 'status', '--porcelain').decode())
        self.assertEqual(git(target, 'show', 'HEAD:app.py').decode(), 'def answer(): return 0\n')

    def test_fake_invocation_copy_boundaries_and_export(self):
        source = self.materialized()
        before = inventory(source, allow_git=True)
        runner = SimulatedDocker()
        output = self.root / 'result'
        result = run_agent(source, 'Return the right answer', output, image='fixture', sandbox=runner)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['completion']['historical_solution_seen'], 'no')
        self.assertEqual(inventory(source, allow_git=True), before)
        self.assertTrue(runner.removed)
        spec = runner.spec
        self.assertEqual(spec.network, 'bridge')
        self.assertFalse(spec.retain_logs)
        self.assertIsNone(spec.broker)
        self.assertNotEqual(spec.workspace, source)
        self.assertFalse(spec.workspace.exists())
        self.assertFalse(spec.inputs.exists())
        unpack(output / 'workspace.tar.gz', self.root / 'export')
        self.assertTrue((self.root / 'export/mineval-fake-result.txt').is_file())
        self.assertFalse((self.root / 'export/.git').exists())
        self.assertEqual(set(p.name for p in output.iterdir()), {'workspace.tar.gz', 'result.json'})

    def test_fake_error_timeout_interrupt_and_unsafe_export_cleanup(self):
        source = self.materialized()
        for outcome, expected in [('error', 'error'), ('timeout', 'timeout')]:
            runner = SimulatedDocker()
            result = run_agent(source, 'fixture', self.root / outcome, image='fixture',
                               adapter=FakeAdapter(outcome), sandbox=runner)
            self.assertEqual(result['status'], expected)
            self.assertTrue(runner.removed)
            self.assertFalse(runner.spec.workspace.exists())
        for runner, error in [(SimulatedDocker(interrupt=True), KeyboardInterrupt),
                              (SimulatedDocker(bad_output=True), ValueError)]:
            with self.assertRaises(error):
                run_agent(source, 'fixture', self.root / 'bad', image='fixture', sandbox=runner)
            self.assertTrue(runner.removed)
            self.assertFalse(runner.spec.workspace.exists())
            self.assertFalse((self.root / 'bad').exists())

    def test_modern_clone_links_and_git_configuration_rejected_before_docker(self):
        for source in [self.repo, self.workspace]:
            runner = SimulatedDocker()
            with self.assertRaises(ValueError):
                run_agent(source, 'fixture', self.root / 'bad', image='fixture', sandbox=runner)
            self.assertIsNone(runner.spec)
        source = self.materialized()
        (source / '.git/config').write_text('[include]\npath = /host/secret\n')
        with self.assertRaisesRegex(ValueError, 'configuration'):
            run_agent(source, 'fixture', self.root / 'bad', image='fixture', sandbox=SimulatedDocker())

    def test_codex_luna_prompt_delivery_and_auth_allowlist_without_launch(self):
        inputs = self.root / 'inputs'; inputs.mkdir()
        command = CodexAdapter().stage(inputs)
        self.assertEqual(command[-1], 'gpt-6-luna')
        for flag in ['--ephemeral', '--ignore-user-config', '--ignore-rules', '--cd /workspace',
                     '--output-schema', '> /dev/null 2>&1']:
            self.assertIn(flag, command[2])
        auth = self.root / 'auth.json'
        write_json(auth, {'tokens': {'access_token': 'synthetic', 'refresh_token': 'synthetic'}})
        provision_auth(auth, inputs / 'auth.json')
        self.assertEqual((inputs / 'auth.json').stat().st_mode & 0o777, 0o600)
        write_json(auth, {'tokens': {'access_token': 'synthetic'}, 'github_token': 'forbidden'})
        with self.assertRaises(ValueError):
            provision_auth(auth, inputs / 'bad.json')

    def test_docker_mounts_only_disposable_dirs_and_disables_transcript_logs(self):
        source = self.materialized()
        runner = SimulatedDocker()
        run_agent(source, 'fixture', self.root / 'result', image='fixture', sandbox=runner)
        # Paths have been cleaned; recreate an equivalent spec while testing Docker argv.
        from dataclasses import replace
        sandbox = DockerSandbox()
        sandbox._cli = Mock()
        sandbox._cli.json.side_effect = [READY, [{'Id': IMAGE_ID, 'Config': {}}]]
        sandbox._cli.call.return_value = (CONTAINER_ID, '', False)
        spec = replace(runner.spec, workspace=source, control=self.root, inputs=self.root)
        with sandbox.container(spec):
            pass
        args = sandbox._cli.call.call_args_list[0].args
        self.assertIn('--log-driver=none', args)
        self.assertNotIn('--log-driver=local', args)
        self.assertTrue(any('dst=/mineval-input' in x and x.endswith(',readonly') for x in args))
        self.assertEqual(args.count('--mount'), 3)
        self.assertFalse(any('docker.sock' in x or '/home/relic' in x for x in args))


@unittest.skipUnless(os.environ.get('MINEVAL_AGENT_TEST_IMAGE'),
                     'set MINEVAL_AGENT_TEST_IMAGE for live rootless fake adapter check')
class LiveAgentTests(HistoryFixture):
    def test_live_fake_copy_and_cleanup(self):
        version = self.store.finalize('example', self.review)['version']
        source = self.root / 'historical'
        self.store.materialize('example', version, source)
        before = inventory(source, allow_git=True)
        from contextlib import contextmanager
        class InspectedSandbox(DockerSandbox):
            ids = []
            @contextmanager
            def container(self, spec):
                with super().container(spec) as container:
                    self.ids.append(container.id)
                    config = self._cli.json('inspect', container.id)[0]
                    self_outer.assertEqual(config['HostConfig']['LogConfig']['Type'], 'none')
                    mounts = {m['Destination']: m for m in config['Mounts']}
                    self_outer.assertEqual(set(mounts), {'/workspace', '/mineval-input',
                                                        '/mineval-control'})
                    self_outer.assertEqual(set(config['HostConfig']['Tmpfs']), {'/tmp'})
                    for destination, path in (('/workspace', spec.workspace),
                                              ('/mineval-input', spec.inputs),
                                              ('/mineval-control', spec.control)):
                        self_outer.assertEqual(mounts[destination]['Source'], str(path))
                        self_outer.assertNotEqual(path, source)
                        self_outer.assertNotIn(fixture_store, path.parents)
                    self_outer.assertFalse(mounts['/mineval-input']['RW'])
                    yield container
        self_outer = self
        fixture_store = self.store.root
        runner = InspectedSandbox(context=os.environ.get('MINEVAL_TEST_CONTEXT'))
        for outcome, expected in [('success', 'completed'), ('error', 'error'), ('timeout', 'timeout')]:
            with self.subTest(outcome=outcome):
                result = run_agent(source, 'Isolated synthetic fixture', self.root / outcome,
                                   image=os.environ['MINEVAL_AGENT_TEST_IMAGE'],
                                   adapter=FakeAdapter(outcome), sandbox=runner,
                                   limits=Limits(timeout_seconds=0.2 if outcome == 'timeout' else 30))
                self.assertEqual(result['status'], expected, result)
                self.assertEqual(inventory(source, allow_git=True), before)
        remaining, _, _ = runner._cli.call('ps', '--all', '--quiet', '--no-trunc')
        for container_id in runner.ids:
            self.assertNotIn(container_id, remaining.splitlines())
