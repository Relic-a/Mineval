"""Admission estimates and best-effort local cgroup v2 measurements.

Docker limits remain the isolation boundary. Admission uses available memory (which
already excludes live allocations), reserving only unconsumed estimates and growth.
"""
import math
import threading
import time
from mineval.core.sandbox_metrics import active_usage, capacity, pressure_sources, pressure_totals, pressure_delta, Measurement


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


def stage_config(root, job, phase):
    """Read the configuration used by this stage in its execution store."""
    from mineval.core.artifacts import read_json
    from mineval.core.store import identifier

    if phase == 'evaluation':
        return job['payload'].get('evaluation_config', {})
    if phase == 'prepare':
        path = root / 'drafts' / identifier(job['task']) / 'control/verify.json'
    elif phase == 'benchmark' and job['payload'].get('selection'):
        version = identifier(job['payload']['selection']['version'])
        path = root / 'tasks' / identifier(job['task']) / 'versions' / version / 'verify-config.json'
    else:
        return {}
    try:
        return read_json(path)
    except FileNotFoundError:
        # Missing task data is handled by the stage itself.
        return {}


def service_budget(config, count):
    """Each overlapping project sandbox starts its own service group."""
    from mineval.core.sandbox_types import Limits

    limits = [Limits(**service.get('limits', {})) for service in config.get('services', ())]
    return count * sum(limit.cpus for limit in limits), count * sum(limit.memory_mb for limit in limits)


def pressure():
    """Maximum host/ancestor PSI some avg10, used by the existing policy."""
    sources = pressure_sources()
    return {resource: max((v[resource] for v in sources.values() if v[resource] is not None), default=0)
            for resource in ('cpu', 'memory')}



class Usage:
    """One stage's live containers, including overlapping verification sandboxes."""
    local = active_usage

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
