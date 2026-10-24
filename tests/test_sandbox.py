import subprocess
import unittest
from unittest.mock import Mock, patch

from mineval.cli import main
from mineval.sandbox import Container, DockerCLI, DockerSandbox, Limits, SandboxError, SandboxSpec


IMAGE_ID = "sha256:" + "a" * 64
CONTAINER_ID = "b" * 64
READY = {
    "SecurityOptions": ["name=seccomp,profile=builtin", "name=rootless"],
    "OSType": "linux", "CgroupVersion": "2", "CgroupDriver": "systemd",
    "MemoryLimit": True, "SwapLimit": True, "CpuCfsQuota": True, "PidsLimit": True,
    "ServerVersion": "test",
}


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = DockerSandbox(context="rootless")
        self.cli = Mock()
        self.sandbox._cli = self.cli
        self.cli.call.return_value = (CONTAINER_ID + "\n", "", False)
        self.cli.json.side_effect = lambda *args: (
            READY.copy() if args[0] == "info" else
            [{"Id": IMAGE_ID, "Config": {}}] if args[0] == "image" else
            {"Running": False, "ExitCode": 7, "OOMKilled": False})
        self.spec = SandboxSpec("example:tag", ("sh", "-c", "exit 7"))

    def test_rootful_and_missing_controllers_rejected_before_create(self):
        for changes in ({"SecurityOptions": ["name=userns"]}, {"CgroupVersion": "1"},
                        {"CgroupDriver": "none"}, {"OSType": "windows"},
                        *({key: False} for key in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit"))):
            with self.subTest(changes=changes):
                self.cli.json.side_effect = None
                self.cli.json.return_value = READY | changes
                with self.assertRaises(SandboxError):
                    self.sandbox.run(self.spec)
                self.cli.call.assert_not_called()

    def test_policy_and_pinned_image_and_exact_argv(self):
        spec = SandboxSpec("example:tag", ("sh", "-c", "echo '$HOME; $(id)'"))
        with self.sandbox.container(spec) as container:
            self.assertEqual(container.id, CONTAINER_ID)
        create = self.cli.call.call_args_list[0].args
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges=true", "--pull=never", "--init",
                     "--restart=no", "--no-healthcheck", "--workdir=/workspace"):
            self.assertIn(flag, create)
        self.assertEqual(create[-5:], ("--entrypoint", "sh", IMAGE_ID, "-c", "echo '$HOME; $(id)'"))
        self.assertNotIn("--volume", create)
        self.assertNotIn("--privileged", create)
        self.assertIn("--log-opt=compress=false", create)
        self.assertEqual(create[create.index("--memory") + 1], "1024m")
        self.assertEqual(create[create.index("--memory-swap") + 1], "1024m")
        self.assertEqual(create[create.index("--cpus") + 1], "2")
        self.assertEqual(create[create.index("--pids-limit") + 1], "256")
        self.assertIn("/workspace:rw,nosuid,nodev,size=512m,mode=1777", create)
        self.assertIn("/tmp:rw,nosuid,nodev,size=64m,mode=1777", create)
        name = create[create.index("--name") + 1]
        self.cli.call.assert_called_with("rm", "--force", "--volumes", name)

    def test_nonzero_workload_is_result_and_removed(self):
        def call(*args, **kwargs):
            return ("output", "error", True) if args[0] == "logs" else (CONTAINER_ID, "", False)
        self.cli.call.side_effect = call
        result = self.sandbox.run(self.spec)
        self.assertEqual(result.exit_code, 7)
        self.assertEqual(result.image_id, IMAGE_ID)
        self.assertEqual(result.container_id, CONTAINER_ID)
        self.assertEqual((result.stdout, result.stderr), ("output", "error"))
        self.assertTrue(result.logs_truncated)
        self.assertFalse(result.timed_out)
        self.assertEqual(self.cli.call.call_args.args[0], "rm")

    def test_timeout_kills_waits_and_returns_result(self):
        waits = 0
        def call(*args, **kwargs):
            nonlocal waits
            if args[0] == "wait":
                waits += 1
                if waits == 1:
                    raise subprocess.TimeoutExpired("docker wait", 0.01)
            return CONTAINER_ID, "", False
        self.cli.call.side_effect = call
        result = self.sandbox.run(self.spec)
        self.assertTrue(result.timed_out)
        self.assertEqual(waits, 2)
        self.cli.call.assert_any_call("kill", CONTAINER_ID)
        self.assertEqual(self.cli.call.call_args.args[0], "rm")

    def test_timeout_race_with_completed_workload(self):
        self.cli.call.side_effect = [subprocess.TimeoutExpired("wait", 1), SandboxError("already stopped"), ("0", "", False)]
        self.assertTrue(Container(self.cli, CONTAINER_ID, IMAGE_ID).wait(1))

    def test_error_or_interrupt_cleans_up(self):
        for error in (SandboxError("start failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error)):
                self.cli.call.reset_mock()
                with self.assertRaises(type(error)):
                    with self.sandbox.container(self.spec):
                        raise error
                self.assertEqual(self.cli.call.call_args.args[0], "rm")

    def test_ambiguous_create_failure_still_cleans_by_name(self):
        self.cli.call.side_effect = [subprocess.TimeoutExpired("create", 30), ("", "", False)]
        with self.assertRaises(subprocess.TimeoutExpired):
            self.sandbox.run(self.spec)
        create = self.cli.call.call_args_list[0].args
        self.cli.call.assert_called_with("rm", "--force", "--volumes", create[create.index("--name") + 1])

    def test_cleanup_failure_is_visible_without_hiding_original_error(self):
        self.cli.call.side_effect = [(CONTAINER_ID, "", False), SandboxError("daemon down")]
        original = ValueError("work failed")
        with self.assertRaises(ValueError) as caught:
            with self.sandbox.container(self.spec):
                raise original
        self.assertIs(caught.exception, original)
        self.assertIn("daemon down", original.__notes__[0])

    def test_cleanup_failure_after_success_raises(self):
        self.cli.call.side_effect = [(CONTAINER_ID, "", False), SandboxError("daemon down")]
        with self.assertRaisesRegex(SandboxError, "daemon down"):
            with self.sandbox.container(self.spec):
                pass

    def test_image_volume_and_missing_image_do_not_create(self):
        for image in ([{"Id": IMAGE_ID, "Config": {"Volumes": {"/data": {}}}}], SandboxError("missing image")):
            self.cli.json.side_effect = [READY, image]
            with self.assertRaises(SandboxError):
                self.sandbox.run(self.spec)
            self.cli.call.assert_not_called()

    def test_limits_and_command_validation(self):
        for kwargs in ({"cpus": 0}, {"cpus": float("nan")}, {"timeout_seconds": float("inf")},
                       {"pids": -1}, {"memory_mb": True}, {"tmp_mb": 0}, {"workspace_mb": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Limits(**kwargs)
        for image, command in (("-x", ("true",)), ("a b", ("true",)), ("a", ()),
                               ("a", "echo x"), ("a", ("echo", "\0"))):
            with self.subTest(image=image, command=command), self.assertRaises(ValueError):
                SandboxSpec(image, command)


class CLITests(unittest.TestCase):
    @patch("mineval.cli.DockerSandbox")
    def test_invalid_spec_returns_infrastructure_status(self, sandbox):
        with patch("sys.stderr"):
            self.assertEqual(main(["run", "--image", "example", "--cpus", "0", "--", "true"]), 125)
        sandbox.return_value.run.assert_not_called()

    @patch("mineval.cli.DockerSandbox")
    def test_interruption_reports_failed_cleanup(self, sandbox):
        error = KeyboardInterrupt()
        error.add_note("Cleanup of mineval-test failed: daemon down")
        sandbox.return_value.run.side_effect = error
        with patch("sys.stderr") as stderr:
            self.assertEqual(main(["run", "--image", "example", "--", "true"]), 130)
        self.assertTrue(any("Cleanup of mineval-test" in str(call) for call in stderr.write.call_args_list))


class DockerCLITests(unittest.TestCase):
    @patch("subprocess.run")
    def test_no_shell_context_and_capture_bound(self, run):
        def subprocess_run(argv, **kwargs):
            self.assertEqual(argv, ["docker", "--context", "rootless", "logs", "container"])
            self.assertNotIn("shell", kwargs)
            kwargs["stdout"].write(b"abcdef")
            kwargs["stderr"].write(b"error")
            return subprocess.CompletedProcess(argv, 0)
        run.side_effect = subprocess_run
        self.assertEqual(DockerCLI("rootless").call("logs", "container", limit=4), ("abcd", "erro", True))

    @patch("subprocess.run", side_effect=FileNotFoundError("docker"))
    def test_missing_docker_is_actionable(self, run):
        with self.assertRaisesRegex(SandboxError, "Cannot execute Docker"):
            DockerCLI().call("info")

    @patch.object(DockerCLI, "call", return_value=("not json", "", False))
    def test_malformed_json(self, call):
        with self.assertRaises(SandboxError):
            DockerCLI().json("info")



class WorkspacePolicyTests(unittest.TestCase):
    def test_only_explicit_directories_mounted_with_selected_network(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            sandbox = DockerSandbox()
            sandbox._cli = Mock()
            sandbox._cli.json.side_effect = [READY, [{'Id': IMAGE_ID, 'Config': {}}]]
            sandbox._cli.call.return_value = (CONTAINER_ID, '', False)
            spec = SandboxSpec('local', ('true',), workspace=Path(temp), network='bridge')
            with sandbox.container(spec):
                pass
            args = sandbox._cli.call.call_args_list[0].args
            self.assertIn('--network=bridge', args)
            self.assertIn(f'type=bind,src={temp},dst=/workspace,bind-propagation=rprivate', args)
            self.assertFalse(any('docker.sock' in arg for arg in args))
            self.assertFalse(any('/workspace:rw' in arg for arg in args))
        for kwargs in ({'network': 'host'}, {'workdir': '/workspace-other'}, {'workdir': '/workspace/../private'}):
            with self.assertRaises(ValueError):
                SandboxSpec('local', ('true',), **kwargs)

    def test_exec_returns_failures_and_timeout_kills_container(self):
        import sys
        cli = DockerCLI()
        cli.prefix = [sys.executable, '-c', "import sys; print('out'); sys.stderr.write('failure'); sys.exit(1)"]
        cli.call = Mock(return_value=('', '', False))
        cli.json = Mock(return_value={'Running': True, 'OOMKilled': False})
        container = Container(cli, CONTAINER_ID, IMAGE_ID)
        result = container.exec(['test'], timeout=1)
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(result['stderr'], 'failure')
        cli.prefix = [sys.executable, '-c', 'import time; time.sleep(10)']
        result = container.exec(['sleep'], timeout=0.05)
        self.assertTrue(result['timed_out'])
        cli.call.assert_any_call('kill', CONTAINER_ID)
        cli.call.assert_any_call('wait', CONTAINER_ID)
        cli.prefix = [sys.executable, '-c', "print('x' * (2 * 1024 * 1024))"]
        result = container.exec(['verbose'], timeout=2)
        self.assertTrue(result['logs_truncated'])
        self.assertEqual(len(result['stdout']), 64 * 1024)

    def test_workspace_export_pauses_copies_and_does_not_mount_archive(self):
        import tempfile
        from pathlib import Path
        from mineval.artifacts import digest, unpack
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / 'workspace'
            workspace.mkdir()
            (workspace / 'result.txt').write_text('agent result')
            cli = Mock()
            cli.json.return_value = {'Running': True, 'Paused': False}
            container = Container(cli, CONTAINER_ID, IMAGE_ID, workspace)
            result = container.export_workspace(Path(temp) / 'result.tar.gz')
            self.assertEqual(result['sha256'], digest(result['path']))
            cli.call.assert_any_call('pause', CONTAINER_ID)
            cli.call.assert_any_call('unpause', CONTAINER_ID)
            unpack(result['path'], Path(temp) / 'copy')
            self.assertEqual((Path(temp) / 'copy/result.txt').read_text(), 'agent result')
            with self.assertRaises(ValueError):
                container.export_workspace(workspace / 'nested.tar.gz')


if __name__ == "__main__":
    unittest.main()
