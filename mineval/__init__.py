"""Historical and Arena benchmarks for coding models."""
from mineval.core.sandbox import DockerSandbox, Limits, RunResult, SandboxError, SandboxSpec

__all__ = ["Store", "DockerSandbox", "Limits", "RunResult", "SandboxError", "SandboxSpec"]


def __getattr__(name):
    if name == "Store":
        from mineval.preparation import Store
        return Store
    raise AttributeError(name)
