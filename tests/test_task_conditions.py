from contextlib import nullcontext
import subprocess
import threading
import unittest
from unittest.mock import Mock

from mineval import benchmark
from mineval.artifacts import inventory
from mineval.repository import future_tree
from mineval.sandbox import Limits, SandboxError, SandboxSpec
from mineval.service_dependencies import group
from test_agents import SimulatedDocker
from test_preparation import FakeRunner, HistoryFixture, step
from mineval.verification import diagnose
from mineval.artifacts import read_json, write_json


class RepositoryTests(HistoryFixture):
    def test_symlink_and_pinned_submodule_survive_reconstruction(self):
        module = self.root / 'origin-module'
        module.mkdir()
        def git(path, *args):
            return subprocess.check_output(['git', '-C', str(path), '-c', 'user.name=Test',
                '-c', 'user.email=test@example.org', '-c', 'commit.gpgsign=false', *args],
                stderr=subprocess.DEVNULL, text=True).strip()

        git(module, 'init', '-q')
        (module / 'value').write_text('pinned')
        git(module, 'add', 'value')
        git(module, 'commit', '-qm', 'first')
        pinned = git(module, 'rev-parse', 'HEAD')
        self.git('checkout', '--detach', self.base)
        self.git('-c', 'protocol.file.allow=always', 'submodule', 'add', str(module), 'vendor/dep')
        (self.repo / 'linked').symlink_to('vendor/dep/value')
        self.git('add', '.')
        self.git('commit', '-qm', 'repository with symlink and submodule')
        base = self.git('rev-parse', 'HEAD')
        local = self.repo / 'vendor/dep'
        (local / 'value').write_text('future')
        git(local, 'add', 'value')
        git(local, 'commit', '-qm', 'new module commit')
        head_module = git(local, 'rev-parse', 'HEAD')
        self.git('add', 'vendor/dep')
        (self.repo / 'tests').mkdir()
        (self.repo / 'tests/test_answer.py').write_text('fixture')
        self.git('add', 'tests')
        self.git('commit', '-qm', 'update pinned module')
        head = self.git('rev-parse', 'HEAD')
        (local / 'value').write_text('dirty')
        bare = self.root / 'superproject-only.git'
        git(self.root, 'clone', '--bare', str(self.repo), str(bare))
        with self.assertRaisesRegex(ValueError, f'Cannot preserve submodule vendor/dep at {pinned}'):
            self.store.register(self.candidate | {'task_id': 'missing-module', 'base_commit': base,
                                'head_commit': head, 'base_source': {'type': 'git', 'path': str(bare)}})
        self.store.register(self.candidate | {'task_id': 'module', 'base_commit': base, 'head_commit': head})
        private = self.store.task('module') / 'private'
        work = self.store.draft('module') / 'workspace'
        self.assertEqual(inventory(work)['linked'], {'symlink': 'vendor/dep/value'})
        self.assertEqual((work / 'linked').read_text(), 'pinned')
        self.assertFalse((private / 'future').exists())
        write_json(self.store.draft('module') / 'control/verify.json', read_json(self.config_path))
        diagnose(self.store, 'module', sandbox=FakeRunner([step()]))
        version = self.freeze('module', self.review)['version']
        import shutil
        shutil.rmtree(self.repo)
        shutil.rmtree(module)
        with future_tree(self.store.task('module'), head) as tree:
            self.assertEqual((tree / 'linked').read_text(), 'future')
        output = self.root / 'materialized'
        self.store.materialize('module', version, output)
        self.assertEqual(git(output / 'vendor/dep', 'rev-parse', 'HEAD'), pinned)
        self.assertNotEqual(pinned, head_module)
        self.assertEqual((output / 'linked').read_text(), 'pinned')

    def test_new_schema_requires_native_script(self):
        self.store.register(self.candidate | {'task_id': 'current-schema', 'schema_version': 2, 'category': 'behavior'})
        write_json(self.store.draft('current-schema') / 'control/verify.json', read_json(self.config_path))
        diagnose(self.store, 'current-schema', sandbox=FakeRunner([step()]))
        approval = self.prepare_review('current-schema', self.review)
        with self.assertRaisesRegex(ValueError, 'require a frozen native verification script'):
            self.store.finalize('current-schema', review_id=approval)

    def test_git_intake_names_escaping_link(self):
        self.git('checkout', '--detach', self.base)
        (self.repo / 'outside').symlink_to('../outside')
        self.git('add', 'outside')
        self.git('commit', '-qm', 'escaping link')
        base = self.git('rev-parse', 'HEAD')
        self.git('commit', '--allow-empty', '-qm', 'future')
        with self.assertRaisesRegex(ValueError, 'Escaping symbolic link: outside'):
            self.store.register(self.candidate | {'task_id': 'unsafe', 'base_commit': base,
                                'head_commit': self.git('rev-parse', 'HEAD')})


class RepeatTests(HistoryFixture):
    def test_distinct_runs_and_budget_selection(self):
        version = self.freeze('example', self.review)['version']
        runs = benchmark.run_repeated(self.store, 'example', version, attempts=2,
                                      run_id='batch', sandbox=SimulatedDocker())
        self.assertEqual([r['run_id'] for r in runs], ['batch-001', 'batch-002'])
        different = benchmark.run(self.store, 'example', version, run_id='smaller',
                                  sandbox=SimulatedDocker(), timeout=4)
        def ref(run):
            return {'task_id': 'example', 'run_id': run['run_id']}
        comparison = benchmark.compare(self.store, {'left': [*map(ref, runs), ref(different)],
                                                     'right': [ref(runs[0])]})
        self.assertEqual(comparison['coverage']['common'], 1)
        self.assertEqual(len(comparison['selections']['left']), 3)
        self.assertEqual(comparison['excluded'][0]['run_id'], 'smaller')
        self.assertIn('conditions', comparison['excluded'][0]['reason'])
        self.assertEqual(comparison['excluded'][0]['different_fields'], ['limits.timeout_seconds'])
        self.assertEqual(different['operation']['limits']['timeout_seconds'], 4)
        self.assertEqual(different['operation']['limits']['cpus'], 2)
        self.assertEqual(different['operation']['limits']['memory_mb'], 2048)
        self.assertTrue(different['agent_result']['image_id'])

    def test_queue_enqueues_each_slot_per_model(self):
        from mineval.workflows import Workflows
        version = self.freeze('example', self.review)['version']
        flow = Workflows(self.store)
        models = [{'model': model, 'adapter': 'fake'} for model in ('a', 'b')]
        jobs = flow.enqueue('benchmark', [{'task_id': 'example', 'version': version}], models, attempts=3)
        self.assertEqual([job['state'] for job in jobs], ['queued'] * 6)
        self.assertEqual(len({job['payload']['run_id'] for job in flow.jobs()}), 6)


class ServiceLifecycleTests(unittest.TestCase):
    def test_success_failure_and_cancellation_clean_up(self):
        image = 'sha256:' + 'a' * 64
        service = {'name': 'db', 'image': image, 'ready': ['probe'], 'limits': {'timeout_seconds': .01}}
        for outcome in ('ready', 'unready', 'cancelled'):
            with self.subTest(outcome=outcome):
                cli = Mock()
                runner = Mock(_cli=cli, cancel_event=threading.Event())
                runner.image.return_value = {'Id': image}
                if outcome == 'cancelled':
                    runner.cancel_event.set()
                if outcome == 'unready':
                    def fail(*args, **kwargs):
                        if args[0] == 'exec':
                            raise SandboxError('not ready')
                    cli.call.side_effect = fail
                context = {'ready': nullcontext(), 'unready': self.assertRaises(SandboxError),
                           'cancelled': self.assertRaises(KeyboardInterrupt)}[outcome]
                with context:
                    with group(runner, SandboxSpec(image, ['true'], services=(service,))):
                        self.assertEqual(outcome, 'ready')
                calls = [entry.args for entry in cli.call.call_args_list]
                self.assertEqual(calls[-2][:3], ('rm', '--force', '--volumes'))
                self.assertEqual(calls[-1][:2], ('network', 'rm'))
        with self.assertRaisesRegex(ValueError, 'pinned image'):
            SandboxSpec(image, ['true'], Limits(), services=(service | {'image': 'postgres:latest'},))
