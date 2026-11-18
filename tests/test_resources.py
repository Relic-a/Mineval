"""Admission accounting and bounded concurrency; no Docker or model calls."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from mineval import resources
from mineval.artifacts import write_json
from mineval.preparation import Store
from mineval.remote_coordinator import Coordinator
from mineval.workflows import Workflows
from mineval.sandbox import Container, DockerSandbox, SandboxError


class ResourceTests(unittest.TestCase):
    def usage(self, current=1024, charged=1024, **extra):
        usage = resources.Usage()
        usage.add(dict(active=True, memory_mb=current, charged_mb=charged,
                       sampled_at=time.monotonic(), **extra))
        return usage

    def admit(self, usage, available, **kw):
        with patch.object(resources, 'capacity', return_value=(8, available)), \
             patch.object(resources, 'pressure', return_value={'cpu': 0, 'memory': 0}):
            return resources.admits({}, [(2, 2048, usage)], (2, 2048),
                                    now=10, last_start=kw.get('last_start', 0))

    def test_available_memory_already_excludes_live_allocations(self):
        # 6 GiB initially, 1 GiB allocated, now 5 GiB available: the active
        # estimate has only 1 GiB unconsumed, not another full 2 GiB.
        self.assertTrue(self.admit(self.usage(), 4000))
        self.assertFalse(self.admit(self.usage(), 3500))

    def test_spike_allowance_blocks_admission_even_with_low_current_usage(self):
        usage = self.usage(current=2048, charged=2048)
        self.assertEqual(usage.remaining(2048, resources.DEFAULTS), 1280)
        self.assertFalse(self.admit(usage, 3800))
        self.assertTrue(self.admit(usage, 4000))

    def test_observed_peak_stays_reserved_after_usage_drops(self):
        usage = self.usage(current=1024, charged=1024, peak_memory_mb=4096)
        self.assertEqual(usage.remaining(2048, resources.DEFAULTS), 3072)
        self.assertFalse(self.admit(usage, 5500))

    def test_unknown_stale_and_cache_measurements_get_no_unearned_credit(self):
        self.assertFalse(self.admit(resources.Usage(), 4000))
        usage = self.usage(charged=0)
        self.assertFalse(self.admit(usage, 4000))
        usage.containers[0]['sampled_at'] = 0
        self.assertFalse(self.admit(usage, 4000))

    def test_nested_containers_and_future_broker_capacity(self):
        usage = resources.Usage(expected_containers=2)
        usage.add(self.usage().containers[0])
        self.assertEqual(usage.remaining(4096, resources.DEFAULTS), 3072)
        usage.add(self.usage().containers[0])
        self.assertEqual(usage.remaining(4096, resources.DEFAULTS), 2048)
        usage.add(self.usage().containers[0])
        self.assertEqual(usage.remaining(4096, resources.DEFAULTS), 3072)

    def test_psi_never_blocks_admission_but_cpu_and_cooldown_do(self):
        self.assertFalse(self.admit(self.usage(), 20000, last_start=9.5))
        with patch.object(resources, 'capacity', return_value=(8, 20000)), \
             patch.object(resources, 'pressure', return_value={'cpu': 100, 'memory': 100}):
            self.assertTrue(resources.admits({}, [], (2, 2048), now=10, last_start=0))
        with patch.object(resources, 'capacity', return_value=(1, 20000)):
            self.assertFalse(resources.admits({}, [], (2, 2048), now=10, last_start=0))
        self.assertNotIn('memory_pressure_percent', resources.options({'resources': {'memory_pressure_percent': 1}}))

    def test_resource_settings_reject_invalid_values(self):
        for values in ({'unknown': 1}, {'cpus_per_job': True}, {'memory_mb_per_job': 0},
                       {'memory_spike_factor': .5}, {'admission_interval_seconds': 0},
                       {'reserve_memory_mb': -1}, {'cpu_pressure_percent': float('nan')}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                resources.options({'resources': values})

    def test_cgroup_counters_include_kernel_peak_cpu_and_oom(self):
        with tempfile.TemporaryDirectory() as tmp:
            group = Path(tmp)
            files = {'memory.current': str(1024**3), 'memory.peak': str(2 * 1024**3),
                     'memory.stat': f'anon {512 * 1024**2}\nshmem {128 * 1024**2}\nfile 99999\n',
                     'cpu.stat': 'usage_usec 2500000\n', 'memory.events': 'oom 2\noom_kill 1\n'}
            for name, text in files.items():
                (group / name).write_text(text)
            usage = resources.Usage()
            measurement = resources.Measurement(Mock(), usage)
            measurement.group = group
            with patch.object(resources, 'pressure_sources', return_value={'host': {'cpu': 0, 'memory': 2}, 'container': {'cpu': 0, 'memory': 0}}):
                measurement.sample()
            metrics = usage.snapshot()[0]
            self.assertEqual(metrics['peak_memory_mb'], 2048)
            self.assertEqual(metrics['charged_mb'], 640)
            self.assertEqual(metrics['cpu_seconds'], 2.5)
            self.assertEqual(metrics['oom_events'], 1)
            self.assertEqual(metrics['pressure_diagnostics']['end']['host']['memory'], 2)
            for name in files:
                (group / name).unlink()
            measurement.close()  # Exited cgroups are normal, not workload errors.
            self.assertFalse(usage.snapshot()[0]['active'])

    def test_pressure_counter_delta_keeps_missing_or_reset_values_unknown(self):
        before = {'host': {'cpu': 10, 'memory': 20}, 'container': {'cpu': None, 'memory': 5}}
        after = {'host': {'cpu': 15, 'memory': 19}, 'container': {'cpu': 2, 'memory': 8}}
        self.assertEqual(resources.pressure_delta(before, after),
                         {'host': {'cpu': 5, 'memory': None}, 'container': {'cpu': None, 'memory': 3}})

    def test_pressure_diagnostics_show_baseline_peak_and_later_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            group = Path(tmp)
            for name, value in {'memory.current': '104857600', 'memory.peak': '209715200',
                                'memory.max': '2147483648', 'memory.stat': 'anon 1\nshmem 0\n',
                                'cpu.stat': 'usage_usec 1000000\n', 'memory.events': 'oom_kill 0\n'}.items():
                (group / name).write_text(value)
            measurement = resources.Measurement(Mock())
            measurement.group = group
            def source(host, container):
                return {'host': {'cpu': host, 'memory': 0}, 'container': {'cpu': container, 'memory': 0}}
            with patch.object(resources, 'pressure_sources', side_effect=[source(10, 70), source(60, 1), source(5, 2)]):
                measurement.sample(); measurement.sample(); measurement.sample()
            diag = measurement.metrics['pressure_diagnostics']
            self.assertEqual(diag['baseline']['host']['cpu'], 10)
            self.assertEqual(diag['peak']['host']['cpu'], 60)
            self.assertEqual(diag['end']['host']['cpu'], 5)
            self.assertEqual(diag['memory_end']['limit_mb'], 2048)
            self.assertEqual(diag['samples'], 3)

    def test_exec_child_oom_is_infrastructure_even_when_container_survives(self):
        import io
        cli = Mock(prefix=['docker'])
        cli.json.return_value = {'Running': True, 'OOMKilled': False}
        container = Container(cli, 'b' * 64, 'image')
        container.resources['oom_events'] = 1
        process = Mock(stdout=io.BytesIO(b''), stderr=io.BytesIO(b''))
        process.poll.return_value = 1
        process.wait.return_value = 1
        with patch('mineval.sandbox.subprocess.Popen', return_value=process):
            result = container.exec(['test'], timeout=1)
        self.assertTrue(result['oom_killed'])
        self.assertEqual(result['resources']['oom_events'], 1)

    def test_remote_or_unavailable_cgroup_is_not_credited(self):
        container = Mock(id='b' * 64)
        container.inspect.return_value = {'Pid': 123}
        measurement = resources.Measurement(container)
        with patch.object(resources, 'cgroups', return_value=[Path('/sys/fs/cgroup/unrelated')]):
            measurement.start()
        measurement.close()
        self.assertFalse(measurement.metrics['available'])
        self.assertIsNone(measurement.thread)

    def test_sandbox_retains_usage_across_broker_thread(self):
        usage = resources.Usage()
        resources.Usage.local.current = usage
        try:
            sandbox = DockerSandbox()
        finally:
            resources.Usage.local.current = None
        def nested():
            Container(Mock(), 'a' * 64, 'image', usage=sandbox.usage)
        thread = threading.Thread(target=nested)
        thread.start(); thread.join()
        self.assertEqual(len(usage.snapshot()), 1)

    def test_limits_are_not_reservations_and_prepare_includes_broker(self):
        with tempfile.TemporaryDirectory() as tmp:
            flow = Workflows(Store(tmp), {'resources': {'memory_mb_per_job': 1200, 'cpus_per_job': 1}})
            job = {'task': 'fixture', 'payload': {'evaluation_config': {'limits': {'memory_mb': 8192, 'tmp_mb': 8192}}}}
            self.assertEqual(flow.resource_request(job, 'evaluation'), (1, 1200))
            self.assertEqual(flow.resource_request(job, 'prepare'), (1, 2400))


class ServiceReservationTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(tmp)
        self.config = {'limits': {'cpus': 3, 'memory_mb': 4096}, 'services': [
            {'name': 'postgres', 'limits': {'cpus': .5, 'memory_mb': 512}},
            {'name': 'redis'},  # Runtime default quotas also apply to services.
        ]}
        self.job = {'id': 'job1', 'kind': 'benchmark', 'task': 'fixture', 'payload': {
            'selection': {'version': 'v1'}, 'evaluation_config': self.config}}

    def scheduler(self, remote):
        if remote:
            write_json(self.root / 'settings.json', {'resources': {'reservation_percent': 50}})
            return Coordinator(self.root), Store(self.root / 'jobs/job1/input/data')
        store = Store(self.root / 'local')
        return Workflows(store), store

    def install_configs(self, store):
        draft = store.draft('fixture') / 'control/verify.json'
        frozen = store.task('fixture') / 'versions/v1/verify-config.json'
        for path in (draft, frozen):
            path.parent.mkdir(parents=True, exist_ok=True)
        write_json(draft, self.config | {'services': self.config['services'][:1]})
        write_json(frozen, self.config)

    def test_stage_reservations_include_explicit_and_default_service_limits(self):
        for remote, benchmark, evaluation, prepare, review in (
                (False, (4.5, 4608), (4, 4096), (3, 5120), (2, 2048)),
                (True, (2.75, 3328), (2.5, 3072), (3.5, 4608), (1, 1024))):
            with self.subTest(remote=remote):
                flow, store = self.scheduler(remote)
                self.install_configs(store)
                evaluation_config = self.config | {'services': self.config['services'][1:]}
                job = self.job | {'payload': self.job['payload'] | {'evaluation_config': evaluation_config}}
                self.assertEqual(flow.resource_request(job, 'benchmark'), benchmark)
                self.assertEqual(flow.resource_request(job, 'evaluation'), evaluation)
                self.assertEqual(flow.resource_request(self.job, 'prepare'), prepare)
                self.assertEqual(flow.resource_request(self.job, 'review'), review)

    def test_local_and_vps_admission_wait_for_service_cpu_and_memory(self):
        for remote, cpu, memory in ((False, 4.5, 4608), (True, 2.75, 3328)):
            with self.subTest(remote=remote):
                flow, store = self.scheduler(remote)
                self.install_configs(store)
                job = self.job
                with flow.db() as db:
                    db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (job['id'], job['kind'], job['task'], json.dumps(job['payload']),
                                'queued', 'queued', '', 0, 0, job['id']))
                def complete(job, phase, usage):
                    flow.update(job['id'], state='completed')
                with patch.object(flow, '_measured_stage', side_effect=complete) as stage, \
                     patch.object(resources, 'capacity') as capacity:
                    for too_small in ((cpu - .25, memory + 512), (cpu, memory + 511)):
                        capacity.return_value = too_small
                        flow._local_work()
                        self.assertEqual(flow.jobs()[0]['state'], 'queued')
                        stage.assert_not_called()
                    capacity.return_value = (cpu, memory + 512)
                    flow._local_work()
                    stage.assert_called_once()
                    self.assertEqual(flow.jobs()[0]['state'], 'completed')


class ProgressiveWorkTests(unittest.TestCase):
    def queued_flow(self, tmp, kinds):
        flow = Workflows(SimpleNamespace(root=Path(tmp)), {
            'resources': {'admission_interval_seconds': .01},
            'concurrency': {'total': 3, 'prepare': 3, 'benchmark': 3}})
        for i, kind in enumerate(kinds):
            with flow.db() as db:
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (str(i), kind, 'fixture', '{}', 'queued', 'queued', '', i, i, str(i)))
        return flow

    def test_preparation_progresses_sequentially_under_soft_resource_constraints(self):
        for capacity, pressure in [((0, 0), {})]:
            with self.subTest(capacity=capacity, pressure=pressure), tempfile.TemporaryDirectory() as tmp:
                flow = self.queued_flow(tmp, ['prepare'] * 3)
                starts, running, peak = [], 0, 0
                def stage(job, phase):
                    nonlocal running, peak
                    running += 1
                    peak = max(peak, running)
                    starts.append(job['id'])
                    time.sleep(.04)  # Allow additional admission checks while active.
                    running -= 1
                    flow.update(job['id'], state='completed')
                with patch.object(flow, '_work_stage', side_effect=stage), \
                     patch.object(resources, 'capacity', return_value=capacity), \
                     patch.object(resources, 'pressure', return_value=pressure):
                    flow.work()
                self.assertEqual(starts, ['0', '1', '2'])
                self.assertEqual(peak, 1)
                self.assertEqual({j['state'] for j in flow.jobs()}, {'completed'})
                self.assertEqual(len(list((Path(tmp) / 'resource-usage').glob('*.json'))), 3)

    def test_preparation_cannot_bypass_admission_while_benchmark_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            flow = self.queued_flow(tmp, ['benchmark', 'prepare'])
            starts = []
            def stage(job, phase):
                starts.append(phase)
                if phase == 'benchmark':
                    time.sleep(.06)
                    self.assertEqual(starts, ['benchmark'])
                flow.update(job['id'], state='completed')
            with patch.object(flow, '_work_stage', side_effect=stage), \
                 patch.object(resources, 'capacity', side_effect=lambda: (8, 16000) if not starts else (0, 0)), \
                 patch.object(resources, 'pressure', return_value={}):
                flow.work()
            self.assertEqual(starts, ['benchmark', 'prepare'])

    def test_preparation_additional_job_uses_progressive_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            flow = self.queued_flow(tmp, ['prepare'] * 2)
            second = threading.Event()
            starts = []
            def stage(job, phase):
                starts.append(time.monotonic())
                if job['id'] == '0':
                    self.assertTrue(second.wait(2), 'spare capacity should admit another job')
                else:
                    second.set()
                flow.update(job['id'], state='completed')
            with patch.object(flow, '_work_stage', side_effect=stage), \
                 patch.object(resources, 'capacity', return_value=(8, 16000)), \
                 patch.object(resources, 'pressure', return_value={}):
                flow.work()
            self.assertEqual(len(starts), 2)
            self.assertGreaterEqual(starts[1] - starts[0], .009)

    def test_first_preparation_runtime_failure_is_visible_and_next_is_attempted(self):
        for failure in (SandboxError('Docker start: insufficient memory'), MemoryError()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as tmp:
                flow = self.queued_flow(tmp, ['prepare'] * 2)
                with patch.object(flow, 'prepare', side_effect=[failure, None]) as prepare, \
                     patch.object(resources, 'capacity', return_value=(0, 0)), \
                     patch.object(resources, 'pressure', return_value={'memory': 99}):
                    flow.work()
                jobs = {j['id']: j for j in flow.jobs()}
                self.assertEqual(prepare.call_count, 2)
                self.assertEqual(jobs['0']['state'], 'failed')
                self.assertIn('Infrastructure failure', jobs['0']['detail'])
                self.assertEqual(jobs['1']['state'], 'completed')

    def test_starts_gradually_rechecks_pressure_and_respects_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            flow = Workflows(SimpleNamespace(root=Path(tmp)), {
                'resources': {'admission_interval_seconds': .04},
                'concurrency': {'total': 2, 'benchmark': 2, 'per_model': 1}})
            for i in range(3):
                with flow.db() as db:
                    db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (str(i), 'benchmark', 'fixture', '{"configuration":{"model":"same"}}',
                                'queued', 'queued', '', i, i, str(i)))
            starts, running, peak = [], 0, 0
            guard = threading.Lock()
            def stage(job, phase):
                nonlocal running, peak
                with guard:
                    starts.append(time.monotonic()); running += 1; peak = max(peak, running)
                time.sleep(.07)
                flow.update(job['id'], state='completed')
                with guard:
                    running -= 1
            with patch.object(flow, '_work_stage', side_effect=stage), \
                 patch.object(resources, 'capacity', return_value=(8, 16000)), \
                 patch.object(resources, 'pressure', return_value={}):
                flow.work()
            self.assertEqual(len(starts), 3)
            self.assertEqual(peak, 1)  # Per-model limit wins over spare capacity.
            self.assertTrue(all(b - a >= .04 for a, b in zip(starts, starts[1:])))
            self.assertEqual(len(list((Path(tmp) / 'resource-usage').glob('*.json'))), 3)
            self.assertEqual({j['state'] for j in flow.jobs()}, {'completed'})

    def test_two_distinct_jobs_overlap_and_pressure_does_not_stop_third(self):
        with tempfile.TemporaryDirectory() as tmp:
            flow = Workflows(SimpleNamespace(root=Path(tmp)), {
                'resources': {'admission_interval_seconds': .01},
                'concurrency': {'total': 3, 'benchmark': 3}})
            for i in range(3):
                with flow.db() as db:
                    db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (str(i), 'benchmark', 'fixture', '{}', 'queued', 'queued', '', i, i, str(i)))
            starts, second = [], threading.Event()
            def stage(job, phase):
                starts.append(job['id'])
                if len(starts) == 1:
                    self.assertTrue(second.wait(2), 'jobs never overlapped')
                    time.sleep(.3)
                else:
                    second.set()
                    time.sleep(.3)
                flow.update(job['id'], state='completed')
            with patch.object(flow, '_work_stage', side_effect=stage), \
                 patch.object(resources, 'capacity', return_value=(8, 16000)), \
                 patch.object(resources, 'pressure', side_effect=lambda: {'memory': 2 if second.is_set() else 0}):
                flow.work()
            self.assertEqual(len(starts), 3)
            self.assertEqual(len(flow.jobs(state='queued')), 0)


class CapacityTests(unittest.TestCase):
    def test_enclosing_cgroup_limits_are_independent_and_use_remaining_ram(self):
        original = Path.read_text
        with tempfile.TemporaryDirectory() as tmp:
            parent, child = Path(tmp), Path(tmp) / 'worker'
            child.mkdir()
            (parent / 'memory.max').write_text(str(4096 * 1024**2))
            (parent / 'memory.current').write_text(str(3072 * 1024**2))
            (parent / 'cpu.max').write_text('5000 100000')
            (child / 'memory.max').write_text('max')
            # A missing child CPU controller must not hide ancestor RAM/CPU.
            def read(path, *args, **kwargs):
                if str(path) == '/proc/meminfo':
                    return 'MemAvailable: 8388608 kB\n'
                return original(path, *args, **kwargs)
            with patch.object(resources, 'cgroups', return_value=[child, parent]), \
                 patch.object(Path, 'read_text', read):
                self.assertEqual(resources.capacity(), (.05, 1024))

    def test_missing_host_memory_fails_closed(self):
        with patch.object(Path, 'read_text', side_effect=OSError), \
             patch.object(resources, 'cgroups', return_value=[]):
            self.assertEqual(resources.capacity()[1], 0)

    def test_telemetry_inspection_failure_does_not_fail_workload_start(self):
        import subprocess
        cli = Mock()
        cli.json.side_effect = subprocess.TimeoutExpired('inspect', 30)
        container = Container(cli, 'b' * 64, 'image')
        container.start()
        container.measurement.close()
        cli.call.assert_called_once_with('start', 'b' * 64)
        self.assertFalse(container.resources['available'])
