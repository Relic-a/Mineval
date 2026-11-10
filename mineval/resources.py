"""Admission estimates and best-effort local cgroup v2 measurements.

Docker limits remain the isolation boundary. Admission uses available memory (which
already excludes live allocations), reserving only unconsumed estimates and growth.
"""
import os
import math
import threading
from . import processes as subprocess
import time
from pathlib import Path


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


DEFAULTS = dict(cpus_per_job=2, memory_mb_per_job=2048, reserve_memory_mb=512,
                admission_interval_seconds=1, memory_spike_mb=256,
                memory_spike_factor=1.5, reservation_percent=80)

# Accepted for existing settings files; PSI is telemetry only.
LEGACY_PRESSURE_SETTINGS = {'cpu_pressure_percent', 'memory_pressure_percent'}


def options(settings):
    supplied = settings.get('resources', {})
    if not isinstance(supplied, dict) or set(supplied) - (DEFAULTS.keys() | LEGACY_PRESSURE_SETTINGS):
        raise ValueError('Unknown resource setting')
    for key, value in supplied.items():
        if key in LEGACY_PRESSURE_SETTINGS and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
            raise ValueError('Legacy pressure settings must be finite nonnegative numbers')
    values = DEFAULTS | {k: v for k, v in supplied.items() if k not in LEGACY_PRESSURE_SETTINGS}
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('Resource budgets must be finite numbers')
        minimum = 1 if key == 'memory_spike_factor' else 0
        if value < minimum or (key in ('cpus_per_job', 'memory_mb_per_job', 'admission_interval_seconds') and value == 0):
            raise ValueError('Resource budgets must be positive; reserves must be nonnegative')
    if not 0 < values['reservation_percent'] <= 100:
        raise ValueError('reservation_percent must be greater than 0 and at most 100')
    return values


def slots(settings):
    cpu, memory = capacity()
    values = options(settings)
    return max(0, min(int(cpu / values['cpus_per_job']),
                      int((memory - values['reserve_memory_mb']) / values['memory_mb_per_job'])))


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


def pressure():
    """Maximum host/ancestor PSI some avg10, used by the existing policy."""
    sources = pressure_sources()
    return {resource: max((v[resource] for v in sources.values() if v[resource] is not None), default=0)
            for resource in ('cpu', 'memory')}



class Usage:
    """One stage's live containers, including overlapping verification sandboxes."""
    local = threading.local()

    def __init__(self, expected_containers=1, values=None):
        self.expected_containers = expected_containers
        self.values = DEFAULTS if values is None else values
        self.lock = threading.Lock()
        self.containers = []

    def add(self, metrics):
        with self.lock:
            self.containers.append(metrics)

    def snapshot(self):
        with self.lock:
            return [dict(item) for item in self.containers]

    def remaining(self, estimate, values):
        live = [item for item in self.snapshot() if item['active']]
        # No container yet (model/network/setup work) still owns its estimate.
        if not live:
            return estimate
        target, credit_total = 0, 0
        for item in live:
            fresh = time.monotonic() - item.get('sampled_at', 0) < 2.5
            current = item.get('memory_mb', 0) if fresh else 0
            credit = item.get('charged_mb', 0) if fresh else 0
            peak = item.get('peak_memory_mb', 0)
            target += max(estimate / self.expected_containers, peak,
                          current * values['memory_spike_factor'] + values['memory_spike_mb'])
            credit_total += credit
        return max(estimate, target) - credit_total


def admits(settings, active, request, *, now, last_start):
    values = options(settings)
    if now - last_start < values['admission_interval_seconds']:
        return False
    cpu, available = capacity()
    requested_cpu, requested_memory = request
    return (sum(c * max(1, sum(item['active'] for item in usage.snapshot()) / usage.expected_containers) for c, m, usage in active) + requested_cpu <= cpu and
            sum(usage.remaining(m, values) for c, m, usage in active) +
            requested_memory + values['reserve_memory_mb'] <= available)


class Measurement:
    """Read kernel counters once a second; no Docker stats processes or profiling runs.

    Missing/remote cgroups leave measurements explicitly unavailable and receive no
    admission credit. Metrics never decide a benchmark verdict on their own.
    """
    def __init__(self, container, usage=None):
        self.container = container
        self.values = usage.values if usage is not None else DEFAULTS
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
