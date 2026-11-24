"""Validated Docker runtime input types and limits."""
from dataclasses import dataclass, field
import math
from pathlib import Path
import re


class SandboxError(RuntimeError):
    """Infrastructure failure, distinct from a workload's nonzero exit code."""


@dataclass(frozen=True)
class Limits:
    cpus: float = 2
    memory_mb: int = 2048
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
    inputs: Path | None = None
    retain_logs: bool = True
    readonly_paths: tuple[str, ...] = ()
    services: tuple[dict, ...] = ()
    environment: dict[str, str] = field(default_factory=dict)
    service_network: str | None = None

    def __post_init__(self) -> None:
        if self.services:
            validate_services(self.services)
        if self.service_network and not re.fullmatch(r'mineval-[0-9a-f]{32}', self.service_network):
            raise ValueError('Invalid service network')
        if self.network not in ("none", "bridge"):
            raise ValueError("network must be none or bridge")
        if not self.workdir.startswith("/workspace") or ".." in self.workdir.split("/") or (self.workdir != "/workspace" and not self.workdir.startswith("/workspace/")):
            raise ValueError("workdir must be inside /workspace")
        for name in ("workspace", "control", "broker", "inputs"):
            value = getattr(self, name)
            if value is not None:
                path = Path(value).resolve(strict=True)
                if not path.is_dir() or "," in str(path):
                    raise ValueError("Mount must be an existing directory without commas")
                object.__setattr__(self, name, path)
        from mineval.core.artifacts import relative
        for name in self.readonly_paths:
            path = relative(name)
            if self.workspace is None or not (self.workspace / path).is_file():
                raise ValueError("Protected checks require regular workspace files")
            current = self.workspace
            for part in path.parts:
                current = current / part
                if current.is_symlink():
                    raise ValueError("Protected checks cannot traverse links")
            if ',' in name:
                raise ValueError("Protected check paths cannot contain commas")
        object.__setattr__(self, "command", command_arguments(self.image, self.command))


def command_arguments(image, command):
    if not isinstance(image, str) or not image or image.startswith("-") or any(c.isspace() or c == "\0" for c in image):
        raise ValueError("image must be a nonempty Docker image reference")
    if isinstance(command, str):
        raise ValueError("command must be an argument sequence, not a shell string")
    command = tuple(command)
    if not command or not command[0] or any(not isinstance(arg, str) or "\0" in arg for arg in command):
        raise ValueError("command must contain an executable and valid string arguments")


    return command


PIN = re.compile(r'(?:sha256:|[^\s@]+@sha256:)[0-9a-f]{64}')


def validate_services(services):
    if not isinstance(services, (list, tuple)) or len(services) > 8:
        raise ValueError('Declare at most eight project services')
    names = set()
    for service in services:
        if not isinstance(service, dict) or set(service) - {
                'name', 'image', 'ready', 'environment', 'argv', 'tmpfs', 'limits'}:
            raise ValueError('Invalid project service fields')
        name = service.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', name) or name in names:
            raise ValueError('Project services require unique DNS names')
        names.add(name)
        image = service.get('image', '')
        if not isinstance(image, str) or not PIN.fullmatch(image):
            raise ValueError(f'Service {name} requires a pinned image digest or ID')
        command_arguments(image, service.get('ready', ()))
        if 'argv' in service:
            command_arguments(image, service['argv'])
        Limits(**service.get('limits', {}))
        env = service.get('environment', {})
        if not isinstance(env, dict) or any(
                not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or not isinstance(value, str) or '\0' in value
                for key, value in env.items()):
            raise ValueError(f'Invalid environment for service {name}')
        paths = service.get('tmpfs', [])
        if not isinstance(paths, list) or len(paths) > 8 or any(
                not isinstance(path, str) or not re.fullmatch(r'(?:/[A-Za-z0-9_.-]+)+', path)
                or any(part in ('.', '..') for part in path.split('/')) for path in paths):
            raise ValueError(f'Invalid tmpfs paths for service {name}')
