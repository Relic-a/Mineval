"""Best-effort cgroup telemetry for individual containers."""
import os
from pathlib import Path
import threading
import time
from mineval.core import processes as subprocess

active_usage = threading.local()


def capacity():
    cpu = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else (os.cpu_count() or 1)
    memory = 0  # Unknown capacity must not admit work.
    try:
        fields = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        memory = int(fields['MemAvailable'].split()[0]) // 1024
    except (OSError, KeyError, ValueError):
        pass
    # Check every enclosing cgroup, including systemd user slices. Docker siblings
    # and other processes may consume the same ancestor budget.
    for group in cgroups():
        try:
            quota = (group / 'cpu.max').read_text().split()
            if quota[0] != 'max':
                cpu = min(cpu, int(quota[0]) / int(quota[1]))
        except (OSError, ValueError, IndexError):
            pass
        try:
            limit = (group / 'memory.max').read_text().strip()
            if limit != 'max':
                used = int((group / 'memory.current').read_text())
                memory = min(memory, max(0, (int(limit) - used) // 1024**2))
        except (OSError, ValueError):
            pass
    return max(0, cpu), max(0, memory)


def cgroups(pid='self'):
    root = Path('/sys/fs/cgroup')
    try:
        relative = next(line[3:] for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines()
                        if line.startswith('0::'))
        group = (root / relative.lstrip('/')).resolve()
        if not group.is_relative_to(root):
            return [root] if pid == 'self' else []
        return [group, *[p for p in group.parents if p.is_relative_to(root)]]
    except (OSError, StopIteration, ValueError):
        return [root] if pid == 'self' else []


def pressure_sources(container_group=None):
    """Bounded PSI samples; labels identify scope without exposing cgroup paths."""
    result = {}
    scopes = [('host', Path('/proc/pressure'))]
    scopes += [(f'ancestor_{i}', group) for i, group in enumerate(cgroups())]
    if container_group is not None:
        scopes.append(('container', container_group))
    for label, base in scopes:
        result[label] = {}
        for resource in ('cpu', 'memory'):
            path = base / resource if label == 'host' else base / (resource + '.pressure')
            try:
                line = next(line for line in path.read_text().splitlines() if line.startswith('some '))
                result[label][resource] = float(dict(item.split('=') for item in line.split()[1:])['avg10'])
            except (OSError, ValueError, KeyError, StopIteration):
                result[label][resource] = None
    return result


def pressure_totals(container_group=None):
    """Cumulative PSI stall microseconds, for command interval deltas only."""
    scopes = [('host', Path('/proc/pressure'))]
    scopes += [(f'ancestor_{i}', group) for i, group in enumerate(cgroups())]
    if container_group is not None:
        scopes.append(('container', container_group))
    result = {}
    for label, base in scopes:
        result[label] = {}
        for resource in ('cpu', 'memory'):
            path = base / resource if label == 'host' else base / (resource + '.pressure')
            try:
                line = next(line for line in path.read_text().splitlines() if line.startswith('some '))
                result[label][resource] = int(dict(item.split('=') for item in line.split()[1:])['total'])
            except (OSError, ValueError, KeyError, StopIteration):
                result[label][resource] = None
    return result


def pressure_delta(start, end):
    return {scope: {key: (value - start[scope][key] if value is not None and
                          start.get(scope, {}).get(key) is not None and value >= start[scope][key] else None)
                    for key, value in readings.items()} for scope, readings in end.items()}


class Measurement:
    """Read kernel counters once a second; no Docker stats processes or profiling runs.

    Missing/remote cgroups leave measurements explicitly unavailable and receive no
    admission credit. Metrics never decide a benchmark verdict on their own.
    """
    def __init__(self, container, usage=None):
        self.container = container
        self.done = threading.Event()
        self.thread = None
        self.group = None
        self.pressure_diagnostics = None
        self.metrics = dict(active=True, available=False, measurement_unavailable='not_started', peak_memory_mb=0,
                            cpu_seconds=0, oom_events=0)
        if usage is not None:
            usage.add(self.metrics)

    def start(self):
        try:
            state = self.container.inspect()
            pid = state.get('Pid')
            groups = cgroups(pid) if type(pid) is int and pid > 0 else []
            # Prevent accidental local PID matches for remote Docker contexts.
            self.metrics['measurement_unavailable'] = 'container_cgroup_unidentified'
            if groups and self.container.id in str(groups[0]):
                self.group = groups[0]
                self.sample()
                self.thread = threading.Thread(target=self.watch, daemon=True)
                self.thread.start()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass

    def sample(self):
        if self.group is None:
            return
        try:
            def number(name):
                return int((self.group / name).read_text())
            def fields(name):
                return dict(line.split() for line in (self.group / name).read_text().splitlines())
            current = number('memory.current') / 1024**2
            stat = fields('memory.stat')
            # File cache may already be included in MemAvailable: only credit
            # anonymous RAM and tmpfs, never reclaimable cache or hard limits.
            charged = (int(stat.get('anon', 0)) + int(stat.get('shmem', 0))) / 1024**2
            peak = current
            try:
                peak = max(peak, number('memory.peak') / 1024**2)
            except OSError:
                pass
            self.metrics.update(available=True, measurement_unavailable=None, memory_mb=current, charged_mb=min(charged, current),
                peak_memory_mb=max(self.metrics['peak_memory_mb'], peak),
                cpu_seconds=int(fields('cpu.stat')['usage_usec']) / 1e6,
                oom_events=max(self.metrics['oom_events'], int(fields('memory.events').get('oom_kill', 0))),
                sampled_at=time.monotonic())
            try:
                limit = (self.group / 'memory.max').read_text().strip()
                limit_mb = None if limit == 'max' else int(limit) / 1024**2
            except (OSError, ValueError):
                limit_mb = None
            memory = {'current_mb': current, 'peak_mb': peak, 'limit_mb': limit_mb,
                      'oom_kill_events': self.metrics['oom_events']}
            sources = pressure_sources(self.group)
            diagnostic = self.pressure_diagnostics
            if diagnostic is None:
                diagnostic = {'baseline': sources, 'peak': sources, 'end': sources,
                              'samples': 0, 'memory_baseline': memory}
            peak_sources = {name: {key: max((x for x in (diagnostic['peak'].get(name, {}).get(key), value)
                                                   if x is not None), default=None)
                                   for key, value in readings.items()}
                            for name, readings in sources.items()}
            self.pressure_diagnostics = diagnostic | {'peak': peak_sources, 'end': sources,
                'samples': diagnostic['samples'] + 1, 'memory_end': memory}
            self.metrics['pressure_diagnostics'] = self.pressure_diagnostics
        except (OSError, ValueError, KeyError):
            pass  # An exited cgroup can disappear before Docker removal.

    def watch(self):
        while not self.done.wait(1):
            self.sample()

    def close(self):
        self.done.set()
        if self.thread:
            self.thread.join()
        self.sample()
        self.metrics['active'] = False
