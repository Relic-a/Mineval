"""Compose the Docker runtime with project services and job telemetry."""
from contextlib import contextmanager
from dataclasses import replace

from mineval.core.container_runtime import Container, ContainerRuntime, DockerCLI, RunResult, log_agent_failure
from mineval.core.sandbox_metrics import active_usage
from mineval.core.sandbox_types import Limits, SandboxError, SandboxSpec
from mineval.core.service_dependencies import group


class DockerSandbox(ContainerRuntime):
    def __init__(self, *, context=None):
        super().__init__(context=context)
        self.usage = getattr(active_usage, 'current', None)

    @contextmanager
    def container(self, spec):
        if spec.services:
            self.check()
            with group(self, spec) as (network, service_images):
                with self.container(replace(spec, services=(), service_network=network)) as container:
                    container.service_images = service_images
                    yield container
            return
        with super().container(spec) as container:
            yield container
