"""Rootless Docker lifecycle, with explicit managed workspace mounts and no pulls."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import re
from pathlib import Path
import shutil
from mineval.core import processes as subprocess
import tempfile
import time
import threading
from typing import Iterator
from uuid import uuid4
from mineval.core.devlog import event


from mineval.core.sandbox_types import Limits, SandboxError, SandboxSpec


@dataclass(frozen=True)
class RunResult:
    container_id: str
    image_id: str
    exit_code: int
    timed_out: bool
    oom_killed: bool
    stdout: str
    stderr: str
    logs_truncated: bool
    duration_seconds: float
    resources: dict = field(default_factory=dict)


class DockerCLI:
    """Use argv without a host shell; bound captured output and command waits."""

    def __init__(self, context: str | None = None):
        self.prefix = ["docker"] + (["--context", context] if context else [])

    def call(self, *args: str, timeout: float = 30, limit: int = 1024 * 1024) -> tuple[str, str, bool]:
        # Files prevent an arbitrary workload log line from filling Python RAM.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                result = subprocess.run(self.prefix + list(args), stdin=subprocess.DEVNULL,
                                        stdout=out, stderr=err, timeout=timeout, check=False)
            except OSError as exc:
                raise SandboxError(f"Cannot execute Docker: {exc}") from exc
            out.seek(0)
            err.seek(0)
            stdout, stderr = out.read(limit + 1), err.read(limit + 1)
        truncated = len(stdout) > limit or len(stderr) > limit
        stdout_text = stdout[:limit].decode("utf-8", errors="replace")
        stderr_text = stderr[:limit].decode("utf-8", errors="replace")
        event('docker.command', exit_code=result.returncode, truncated=truncated,
              level='warning' if result.returncode else 'info',
              sensitive=None if args[0] == 'logs' else
              {'operation': args[0], 'stdout': stdout_text, 'stderr': stderr_text})
        if result.returncode:
            raise SandboxError(f"Docker {args[0]} failed ({result.returncode}): {stderr_text.strip()}")
        return stdout_text, stderr_text, truncated

    def json(self, *args: str):
        output, _, truncated = self.call(*args)
        try:
            if truncated:
                raise ValueError("response exceeded capture limit")
            return json.loads(output)
        except ValueError as exc:
            raise SandboxError(f"Invalid Docker JSON response: {exc}") from exc


class Container:
    """Valid only inside DockerSandbox.container(); removal belongs to its owner."""

    def __init__(self, cli: DockerCLI, container_id: str, image_id: str, workspace: Path | None = None, cancel_event=None, usage=None, retain_logs=True):
        self._cli = cli
        self.id = container_id
        self.image_id = image_id
        self.workspace = workspace
        self.cancel_event = cancel_event
        self.retain_logs = retain_logs
        from mineval.core.sandbox_metrics import Measurement
        self.measurement = Measurement(self, usage)
        self.resources = self.measurement.metrics

    def start(self) -> None:
        self._cli.call("start", self.id)
        event("container.started", container_id=self.id)
        self.measurement.start()

    def wait(self, timeout: float) -> bool:
        """Return True on deadline, killing the entire container before returning."""
        done = threading.Event()
        cancelled = threading.Event()
        def watch():
            while not done.wait(0.1):
                if self.cancel_event.is_set():
                    cancelled.set()
                    try:
                        self._cli.call('kill', self.id)
                    except SandboxError:
                        pass  # Lifecycle removal still owns cleanup on daemon failure.
                    return
        watcher = threading.Thread(target=watch, daemon=True) if self.cancel_event is not None else None
        if watcher:
            watcher.start()
        try:
            self._cli.call("wait", self.id, timeout=timeout)
            if cancelled.is_set():
                raise KeyboardInterrupt()
            return False
        except subprocess.TimeoutExpired:
            event("container.timeout", level="warning", container_id=self.id, timeout_seconds=timeout)
            try:
                self._cli.call("kill", self.id)
            except SandboxError:
                # The workload may have exited between the deadline and kill.
                state = self._cli.json("inspect", "--format", "{{json .State}}", self.id)
                if state["Running"]:
                    raise
            self._cli.call("wait", self.id)
            return True
        finally:
            done.set()
            if watcher:
                watcher.join(timeout=31)

    def logs(self):
        return self._cli.call("logs", self.id, limit=5 * 1024 * 1024)

    def export_workspace(self, destination):
        """Export a consistent result without coupling the lifecycle to a harness."""
        from mineval.core.artifacts import copy_tree, digest, pack
        if self.workspace is None:
            raise SandboxError("Export requires a host-managed workspace")
        destination = Path(destination).resolve()
        if destination == self.workspace or self.workspace in destination.parents or destination.exists():
            raise ValueError("Export destination must be new and outside the workspace")
        state = self.inspect()
        pause = state['Running'] and not state.get('Paused', False)
        with tempfile.TemporaryDirectory() as temp:
            if pause:
                self.pause()
            try:
                copy_tree(self.workspace, Path(temp) / 'workspace')
            finally:
                if pause:
                    self.unpause()
            pack(Path(temp) / 'workspace', destination)
        return {"path": str(destination), "sha256": digest(destination)}

    def inspect(self) -> dict:
        state = self._cli.json("inspect", "--format", "{{json .State}}", self.id)
        self.resources['oom_events'] = max(self.resources['oom_events'], int(bool(state.get('OOMKilled'))))
        event("container.state", container_id=self.id, running=state.get("Running"),
              exit_code=state.get("ExitCode"), oom_killed=state.get("OOMKilled"))
        return state

    def pause(self):
        self._cli.call("pause", self.id)

    def unpause(self):
        self._cli.call("unpause", self.id)

    def exec(self, argv, *, timeout, workdir="/workspace") -> dict:
        """Execute a step; timeout kills the whole sandbox, including descendants."""
        from mineval.core.sandbox_metrics import pressure_totals, pressure_delta
        started = time.monotonic()
        psi_start = pressure_totals(self.measurement.group)
        timed_out = False
        limit = 64 * 1024
        buffers = [bytearray(), bytearray()]
        truncated = [False, False]
        def drain(stream, index):
            with stream:
                for block in iter(lambda: stream.read(65536), b""):
                    remaining = limit - len(buffers[index])
                    buffers[index].extend(block[:remaining])
                    if len(block) > remaining:
                        truncated[index] = True
        try:
            proc = subprocess.Popen(self._cli.prefix + ["exec", "--workdir", workdir, self.id, *argv],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise SandboxError(f"Cannot execute Docker: {exc}") from exc
        threads = [threading.Thread(target=drain, args=(stream, index), daemon=True)
                   for index, stream in enumerate((proc.stdout, proc.stderr))]
        for thread in threads:
            thread.start()
        try:
            try:
                deadline = time.monotonic() + timeout
                while True:
                    if self.cancel_event is not None and self.cancel_event.is_set():
                        self._cli.call('kill', self.id)
                        self._cli.call('wait', self.id)
                        raise KeyboardInterrupt()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired('docker exec', timeout)
                    try:
                        code = proc.wait(timeout=min(0.2, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    self._cli.call("kill", self.id)
                except SandboxError:
                    if self.inspect()["Running"]:
                        raise
                self._cli.call("wait", self.id)
                code = None
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            for thread in threads:
                thread.join(timeout=10)
            if any(thread.is_alive() for thread in threads):
                raise SandboxError("Docker exec output streams did not close")
        stdout, stderr = buffers
        event('container.exec.end', container_id=self.id, exit_code=code, timed_out=timed_out,
              truncated=any(truncated), duration_seconds=time.monotonic() - started,
              level='warning' if code or timed_out else 'info',
              sensitive={'stdout': stdout.decode(errors='replace'), 'stderr': stderr.decode(errors='replace')})
        state = self.inspect()
        self.measurement.sample()
        psi_end = pressure_totals(self.measurement.group)
        return {"resources": dict(self.resources), "pressure_stall_us": pressure_delta(psi_start, psi_end), "started_at_monotonic": started, "ended_at_monotonic": time.monotonic(), "exit_code": code, "timed_out": timed_out, "oom_killed": bool(state.get("OOMKilled") or self.resources["oom_events"]),
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
                "logs_truncated": any(truncated),
                "duration_seconds": time.monotonic() - started}

    def result(self, *, timed_out: bool, duration_seconds: float) -> RunResult:
        state = self.inspect()
        self.measurement.sample()
        if state["Running"]:
            raise SandboxError("Cannot collect a result while the workload is running")
        stdout, stderr, truncated = self._cli.call("logs", self.id) if self.retain_logs else ("", "", False)
        return RunResult(self.id, self.image_id, state["ExitCode"], timed_out,
                         bool(state["OOMKilled"] or self.resources["oom_events"]), stdout, stderr, truncated, duration_seconds, dict(self.resources))


def log_agent_failure(name, container, state, timed_out):
    """Record failure state privately; lifecycle cleanup captures bounded logs."""
    reason = ('timeout' if timed_out else 'oom' if state.get('OOMKilled')
              else 'running' if state.get('Running') else 'nonzero_exit')
    event(name, level='error', container_id=container.id, reason=reason,
          sensitive={'state': state})


class ContainerRuntime:
    _cancellation = threading.local()

    def __init__(self, *, context: str | None = None):
        self._cli = DockerCLI(context)
        self.cancel_event = getattr(self._cancellation, 'event', None)
        self.usage = None
        self.container_inputs = None

    def check(self) -> dict:
        """Fail closed if rootless isolation or resource enforcement is absent."""
        info = self._cli.json("info", "--format", "{{json .}}")
        security = info.get("SecurityOptions") or []
        if not any(option.split(",")[0] == "name=rootless" for option in security):
            raise SandboxError("A rootless Docker daemon is required; select its Docker context")
        if info.get("OSType") != "linux":
            raise SandboxError("Linux containers are required")
        if info.get("CgroupVersion") != "2" or info.get("CgroupDriver") != "systemd":
            raise SandboxError("Rootless resource limits require cgroup v2 and systemd")
        required = ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit")
        missing = [name for name in required if not info.get(name)]
        if missing:
            raise SandboxError("Docker resource controllers unavailable: " + ", ".join(missing))
        return info

    def image(self, reference):
        # Tags are resolved at every launch. Only content-addressed image metadata
        # is reusable; create still uses --pull=never and checks daemon isolation.
        cache = getattr(self, '_images', None)
        if cache is None:
            self._images = cache = {}
        if reference.startswith('sha256:') and reference in cache:
            return cache[reference]
        image = self._cli.json('image', 'inspect', reference)[0]
        cache[image['Id']] = image
        return image

    @contextmanager
    def container(self, spec: SandboxSpec) -> Iterator[Container]:
        if spec.services:
            raise SandboxError("Companion services require the composed Docker runtime")
        self.check()
        image = self.image(spec.image)
        if self.container_inputs is not None:
            with self.container_inputs(self, spec, image) as container:
                yield container
        else:
            with self._container(spec, image=image) as container:
                yield container

    @contextmanager
    def _container(self, spec: SandboxSpec, *, extra_mounts=(), extra_environment=(), lifecycle=None, image=None) -> Iterator[Container]:
        """Create, yield, and remove one container, including anonymous volumes.

        Tmpfs content exists only while running. A managed bind workspace persists
        independently of the container; export_workspace takes a consistent copy. Cleanup failures are raised, or
        attached to an active error.
        """
        if image is None:
            self.check()
            image = self.image(spec.image)
        # VOLUME declarations otherwise silently introduce writable disk storage.
        if image.get("Config", {}).get("Volumes"):
            raise SandboxError("Base images with declared VOLUME paths are unsupported")
        image_id = image["Id"]
        name = "mineval-" + uuid4().hex
        limits = spec.limits
        args = ["create", "--name", name, "--label", "io.mineval.managed=true",
                "--pull=never", "--user=0:0", f"--network={spec.service_network or spec.network}", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges=true", "--init",
                "--restart=no", "--no-healthcheck", "--memory", f"{limits.memory_mb}m",
                "--memory-swap", f"{limits.memory_mb}m", "--cpus", str(limits.cpus),
                "--pids-limit", str(limits.pids),
                "--tmpfs", f"/tmp:rw,nosuid,nodev,size={limits.tmp_mb}m,mode=1777",
                f"--workdir={spec.workdir}", "--env=HOME=/tmp",
                "--log-driver=local", "--log-opt=max-size=5m", "--log-opt=max-file=1",
                "--log-opt=compress=false",
                "--entrypoint", spec.command[0], image_id, *spec.command[1:]]
        position = args.index('--entrypoint')
        args[position:position] = [f'--env={key}={value}' for key, value in spec.environment.items()]
        args[position:position] = extra_environment
        mounts = list(extra_mounts)
        if spec.workspace is None:
            mounts += ["--tmpfs", f"/workspace:rw,nosuid,nodev,size={limits.workspace_mb}m,mode=1777"]
        else:
            mounts += ["--mount", f"type=bind,src={spec.workspace},dst=/workspace,bind-propagation=rprivate"]
        for path, destination in ((spec.control, "/mineval-control"), (spec.broker, "/mineval-broker"), (spec.inputs, "/mineval-input")):
            if path is not None:
                mounts += ["--mount", f"type=bind,src={path},dst={destination},bind-propagation=rprivate" + (",readonly" if destination in ("/mineval-broker", "/mineval-input") else "")]
        for protected_path in spec.readonly_paths:
            mounts += ["--mount", f"type=bind,src={spec.workspace / protected_path},dst=/workspace/{protected_path},readonly,bind-propagation=rprivate"]
        # Insert mounts before the entrypoint/image, never interpret them as argv.
        position = args.index("--entrypoint")
        args[position:position] = mounts
        failure = None
        container = None
        try:
            if lifecycle is not None:
                lifecycle['created'] = True
            output, _, _ = self._cli.call(*args)
            container_id = output.strip()
            if len(container_id) != 64 or any(c not in "0123456789abcdef" for c in container_id):
                raise SandboxError("Docker create did not return a valid container ID")
            container = Container(self._cli, container_id, image_id, spec.workspace, self.cancel_event, self.usage, spec.retain_logs)
            event('container.created', container_id=container_id, sensitive={'image': spec.image})
            yield container
        except BaseException as exc:
            from mineval.core.devlog import failure as log_failure
            log_failure('container.error', exc, container_id=name)
            failure = exc
            raise
        finally:
            if container is not None:
                container.measurement.close()
            # Collect before removal, even on timeout, cancellation or a failed stage.
            # Never put raw workload output in evidence bundles or normal logs.
            if container is not None:
                try:
                    stdout, stderr, truncated = container.logs()
                    event('container.output', container_id=container.id, truncated=truncated,
                          sensitive={'stdout': stdout, 'stderr': stderr})
                except Exception as log_error:
                    from mineval.core.devlog import failure as log_failure
                    log_failure('container.logs_failed', log_error, container_id=container.id)
            try:
                self._cli.call("rm", "--force", "--volumes", name)
                event('container.removed', container_id=name)
                if lifecycle is not None:
                    lifecycle['removed'] = True
            except BaseException as cleanup_error:
                from mineval.core.devlog import failure as log_failure
                log_failure("container.cleanup_failed", cleanup_error, container_id=name)
                if failure is None:
                    raise
                failure.add_note(f"Cleanup of {name} failed: {cleanup_error}")

    def run(self, spec: SandboxSpec) -> RunResult:
        with self.container(spec) as container:
            started = time.monotonic()
            container.start()
            timed_out = container.wait(spec.limits.timeout_seconds)
            return container.result(timed_out=timed_out, duration_seconds=time.monotonic() - started)
