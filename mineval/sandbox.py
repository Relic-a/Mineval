"""Rootless Docker lifecycle, with explicit managed workspace mounts and no pulls."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import subprocess
import tempfile
import time
import threading
from typing import Iterator
from uuid import uuid4


class SandboxError(RuntimeError):
    """Infrastructure failure, distinct from a workload's nonzero exit code."""


@dataclass(frozen=True)
class Limits:
    cpus: float = 2
    memory_mb: int = 1024
    pids: int = 256
    workspace_mb: int = 512
    tmp_mb: int = 64
    timeout_seconds: float = 300

    def __post_init__(self) -> None:
        for name in ("cpus", "timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("memory_mb", "pids", "workspace_mb", "tmp_mb"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class SandboxSpec:
    image: str
    command: tuple[str, ...]
    limits: Limits = field(default_factory=Limits)
    workspace: Path | None = None
    network: str = "none"
    workdir: str = "/workspace"
    control: Path | None = None
    broker: Path | None = None

    def __post_init__(self) -> None:
        if self.network not in ("none", "bridge"):
            raise ValueError("network must be none or bridge")
        if not self.workdir.startswith("/workspace") or ".." in self.workdir.split("/") or (self.workdir != "/workspace" and not self.workdir.startswith("/workspace/")):
            raise ValueError("workdir must be inside /workspace")
        for name in ("workspace", "control", "broker"):
            value = getattr(self, name)
            if value is not None:
                path = Path(value).resolve(strict=True)
                if not path.is_dir() or "," in str(path):
                    raise ValueError("Mount must be an existing directory without commas")
                object.__setattr__(self, name, path)
        if not isinstance(self.image, str) or not self.image or self.image.startswith("-") or any(c.isspace() or c == "\0" for c in self.image):
            raise ValueError("image must be a nonempty Docker image reference")
        if isinstance(self.command, str):
            raise ValueError("command must be an argument sequence, not a shell string")
        object.__setattr__(self, "command", tuple(self.command))
        if not self.command or not self.command[0] or any(not isinstance(arg, str) or "\0" in arg for arg in self.command):
            raise ValueError("command must contain an executable and valid string arguments")


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

    def __init__(self, cli: DockerCLI, container_id: str, image_id: str, workspace: Path | None = None):
        self._cli = cli
        self.id = container_id
        self.image_id = image_id
        self.workspace = workspace

    def start(self) -> None:
        self._cli.call("start", self.id)

    def wait(self, timeout: float) -> bool:
        """Return True on deadline, killing the entire container before returning."""
        try:
            self._cli.call("wait", self.id, timeout=timeout)
            return False
        except subprocess.TimeoutExpired:
            try:
                self._cli.call("kill", self.id)
            except SandboxError:
                # The workload may have exited between the deadline and kill.
                state = self._cli.json("inspect", "--format", "{{json .State}}", self.id)
                if state["Running"]:
                    raise
            self._cli.call("wait", self.id)
            return True

    def logs(self):
        return self._cli.call("logs", self.id)

    def export_workspace(self, destination):
        """Export a consistent result without coupling the lifecycle to a harness."""
        from .artifacts import copy_tree, digest, pack
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
        return self._cli.json("inspect", "--format", "{{json .State}}", self.id)

    def pause(self):
        self._cli.call("pause", self.id)

    def unpause(self):
        self._cli.call("unpause", self.id)

    def exec(self, argv, *, timeout, workdir="/workspace") -> dict:
        """Execute a step; timeout kills the whole sandbox, including descendants."""
        started = time.monotonic()
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
                code = proc.wait(timeout=timeout)
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
        state = self.inspect()
        return {"exit_code": code, "timed_out": timed_out, "oom_killed": state.get("OOMKilled", False),
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
                "logs_truncated": any(truncated),
                "duration_seconds": time.monotonic() - started}

    def result(self, *, timed_out: bool, duration_seconds: float) -> RunResult:
        state = self._cli.json("inspect", "--format", "{{json .State}}", self.id)
        if state["Running"]:
            raise SandboxError("Cannot collect a result while the workload is running")
        stdout, stderr, truncated = self._cli.call("logs", self.id)
        return RunResult(self.id, self.image_id, state["ExitCode"], timed_out,
                         state["OOMKilled"], stdout, stderr, truncated, duration_seconds)


class DockerSandbox:
    def __init__(self, *, context: str | None = None):
        self._cli = DockerCLI(context)

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

    @contextmanager
    def container(self, spec: SandboxSpec) -> Iterator[Container]:
        """Create, yield, and remove one container, including anonymous volumes.

        Tmpfs content exists only while running. A managed bind workspace persists
        independently of the container; export_workspace takes a consistent copy. Cleanup failures are raised, or
        attached to an active error.
        """
        self.check()
        image = self._cli.json("image", "inspect", spec.image)[0]
        # VOLUME declarations otherwise silently introduce writable disk storage.
        if image.get("Config", {}).get("Volumes"):
            raise SandboxError("Base images with declared VOLUME paths are unsupported")
        image_id = image["Id"]
        name = "mineval-" + uuid4().hex
        limits = spec.limits
        args = ["create", "--name", name, "--label", "io.mineval.managed=true",
                "--pull=never", "--user=0:0", f"--network={spec.network}", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges=true", "--init",
                "--restart=no", "--no-healthcheck", "--memory", f"{limits.memory_mb}m",
                "--memory-swap", f"{limits.memory_mb}m", "--cpus", str(limits.cpus),
                "--pids-limit", str(limits.pids),
                "--tmpfs", f"/tmp:rw,nosuid,nodev,size={limits.tmp_mb}m,mode=1777",
                f"--workdir={spec.workdir}", "--env=HOME=/tmp",
                "--log-driver=local", "--log-opt=max-size=1m", "--log-opt=max-file=1",
                "--entrypoint", spec.command[0], image_id, *spec.command[1:]]
        mounts = []
        if spec.workspace is None:
            mounts += ["--tmpfs", f"/workspace:rw,nosuid,nodev,size={limits.workspace_mb}m,mode=1777"]
        else:
            mounts += ["--mount", f"type=bind,src={spec.workspace},dst=/workspace,bind-propagation=rprivate"]
        for path, destination in ((spec.control, "/mineval-control"), (spec.broker, "/mineval-broker")):
            if path is not None:
                mounts += ["--mount", f"type=bind,src={path},dst={destination},bind-propagation=rprivate" + (",readonly" if destination == "/mineval-broker" else "")]
        # Insert mounts before the entrypoint/image, never interpret them as argv.
        position = args.index("--entrypoint")
        args[position:position] = mounts
        failure = None
        try:
            output, _, _ = self._cli.call(*args)
            container_id = output.strip()
            if len(container_id) != 64 or any(c not in "0123456789abcdef" for c in container_id):
                raise SandboxError("Docker create did not return a valid container ID")
            yield Container(self._cli, container_id, image_id, spec.workspace)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                self._cli.call("rm", "--force", "--volumes", name)
            except BaseException as cleanup_error:
                if failure is None:
                    raise
                failure.add_note(f"Cleanup of {name} failed: {cleanup_error}")

    def run(self, spec: SandboxSpec) -> RunResult:
        with self.container(spec) as container:
            started = time.monotonic()
            container.start()
            timed_out = container.wait(spec.limits.timeout_seconds)
            return container.result(timed_out=timed_out, duration_seconds=time.monotonic() - started)
