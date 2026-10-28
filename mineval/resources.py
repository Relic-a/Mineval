"""Conservative admission budgets, independent of configured worker counts."""
import os
import math
from pathlib import Path


def capacity():
    cpu = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else (os.cpu_count() or 1)
    memory = 1024
    try:
        fields = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        memory = int(fields['MemAvailable'].split()[0]) // 1024
        quota = Path('/sys/fs/cgroup/cpu.max').read_text().split()
        if quota[0] != 'max':
            cpu = min(cpu, int(quota[0]) / int(quota[1]))
        limit = Path('/sys/fs/cgroup/memory.max').read_text().strip()
        if limit != 'max':
            used = int(Path('/sys/fs/cgroup/memory.current').read_text())
            memory = min(memory, max(0, (int(limit) - used) // 1024**2))
    except (OSError, KeyError, ValueError):
        pass
    return max(0.1, cpu), max(0, memory)


def slots(settings):
    cpu, memory = capacity()
    options = settings.get('resources', {})
    if not isinstance(options, dict) or set(options) - {'cpus_per_job', 'memory_mb_per_job', 'reserve_memory_mb'}:
        raise ValueError('Unknown resource setting')
    per_cpu = float(options.get('cpus_per_job', 2))
    per_memory = int(options.get('memory_mb_per_job', 2048))
    reserve = int(options.get('reserve_memory_mb', 512))
    if not math.isfinite(per_cpu) or per_cpu <= 0 or per_memory <= 0 or reserve < 0:
        raise ValueError('Resource budgets must be positive; reserve must be nonnegative')
    return max(0, min(int(cpu / per_cpu), int((memory - reserve) / per_memory)))
