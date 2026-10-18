"""Explicitly opt in with a preloaded shell image; never pull or alter the daemon."""

import os
import unittest

from mineval import DockerSandbox, Limits, SandboxSpec


@unittest.skipUnless(os.environ.get("MINEVAL_TEST_IMAGE"), "set MINEVAL_TEST_IMAGE for live rootless checks")
class RootlessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.runner = DockerSandbox(context=os.environ.get("MINEVAL_TEST_CONTEXT"))
        self.runner.check()
        self.image = os.environ["MINEVAL_TEST_IMAGE"]

    def assert_removed(self, container_id):
        ids, _, _ = self.runner._cli.call("ps", "--all", "--quiet", "--no-trunc")
        self.assertNotIn(container_id, ids.splitlines())

    def test_isolation_writes_and_cleanup(self):
        # No image-dependent utilities needed beyond /bin/sh builtins.
        script = """
        printf scratch > /workspace/probe || exit 1
        printf scratch > /tmp/probe || exit 2
        if (printf forbidden > /mineval-readonly-probe) 2>/dev/null; then exit 3; fi
        printf success
        """
        spec = SandboxSpec(self.image, ("/bin/sh", "-c", script))
        with self.runner.container(spec) as container:
            inspected = self.runner._cli.json("inspect", container.id)[0]
            host = inspected["HostConfig"]
            self.assertEqual(host["NetworkMode"], "none")
            self.assertTrue(host["ReadonlyRootfs"])
            self.assertEqual(host["CapDrop"], ["ALL"])
            self.assertIn("no-new-privileges=true", host["SecurityOpt"])
            self.assertFalse(host["Privileged"])
            self.assertFalse(host["Binds"])
            self.assertEqual(host["Memory"], 1024 * 1024 * 1024)
            self.assertEqual(host["MemorySwap"], host["Memory"])
            self.assertEqual(host["NanoCpus"], 2_000_000_000)
            self.assertEqual(host["PidsLimit"], 256)
            container.start()
            self.assertFalse(container.wait(10))
            result = container.result(timed_out=False, duration_seconds=0)
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertEqual(result.stdout, "success")
        self.assert_removed(container.id)

    def test_deadline_and_cleanup(self):
        spec = SandboxSpec(self.image, ("/bin/sh", "-c", "while :; do :; done"),
                           Limits(timeout_seconds=0.2))
        result = self.runner.run(spec)
        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.exit_code, 0)
        self.assert_removed(result.container_id)

@unittest.skipUnless(os.environ.get('MINEVAL_TEST_IMAGE'), 'set MINEVAL_TEST_IMAGE for live rootless checks')
class PreparationIntegrationTests(unittest.TestCase):
    def test_repeated_fresh_diagnostics_and_bind_write_isolation(self):
        from pathlib import Path
        import tempfile
        from mineval.artifacts import write_json
        from mineval.preparation import Store
        from mineval.verification import diagnose
        runner = DockerSandbox(context=os.environ.get('MINEVAL_TEST_CONTEXT'))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / 'data')
            (root / 'base').mkdir()
            (root / 'base/input').write_text('base')
            sha = 'b' * 40
            (root / 'future' / sha).mkdir(parents=True)
            (root / 'future' / sha / 'check.sh').write_text('test -f input')
            store.register({'task_id': 'live', 'repository': 'local:fixture', 'task_summary': 'fixture',
                            'base_commit': 'a' * 40, 'head_commit': sha,
                            'base_source': {'type': 'tree', 'path': str(root / 'base')},
                            'future_source': {'type': 'tree', 'path': str(root / 'future')},
                            'source_attestation': 'Synthetic live integration fixture'})
            write_json(store.draft('live') / 'control/verify.json', {
                'image': os.environ['MINEVAL_TEST_IMAGE'], 'network': 'none',
                'commands': [{'name': 'fresh', 'kind': 'setup', 'argv': ['/bin/sh', '-c', 'test ! -e marker && echo written > marker']},
                             {'name': 'historical-failure', 'kind': 'tests', 'argv': ['/bin/sh', '-c', 'exit 1']}]})
            first = diagnose(store, 'live', sandbox=runner)
            second = diagnose(store, 'live', sandbox=runner)
            self.assertNotEqual(first.get('container_id'), second.get('container_id'))
            for report in (first, second):
                self.assertEqual(report['status'], 'checks_failed', report)
                self.assertEqual([x['status'] for x in report['commands']], ['passed', 'failed'])
            self.assertFalse((store.draft('live') / 'workspace/marker').exists())


if __name__ == "__main__":
    unittest.main()
