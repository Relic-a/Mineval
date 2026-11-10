"""Shared host-wide admission for isolated remote execution stores."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from .artifacts import lock, read_json
from .preparation import Store, identifier
from .workflows import Workflows


class Coordinator(Workflows):
    def __init__(self, root):
        self.remote_coordinator = True
        self.worker_root = Path(root).resolve()
        config = self.worker_root / 'settings.json'
        settings = read_json(config) if config.exists() else {}
        super().__init__(Store(self.worker_root / 'queue'), settings,
                         auth_file=self.worker_root / 'auth.json')

    def resource_request(self, job, phase):
        from .resources import options
        from .sandbox import Limits
        values = options(self.settings)
        task, payload = job['task'], job['payload']
        store = Store(self.worker_root / 'jobs' / identifier(job['id']) / 'input' / 'data')
        config = {}
        if phase == 'prepare':
            path = store.draft(task) / 'control/verify.json'
            if path.exists():
                config = read_json(path)
        elif phase == 'benchmark':
            path = store.task(task) / 'versions' / payload['selection']['version'] / 'verify-config.json'
            config = read_json(path)
        elif phase == 'evaluation':
            config = payload.get('evaluation_config', {})
        limits = Limits(**config.get('limits', {}))
        count = 2 if phase == 'prepare' else 1
        fraction = values['reservation_percent'] / 100
        return count * limits.cpus * fraction, count * limits.memory_mb * fraction

    def _measured_stage(self, job, phase, usage):
        # Each execution has its own staged task data, but all stage transitions
        # and resource admission go through this one shared queue.
        directory = self.worker_root / 'jobs' / identifier(job['id']) / 'input'
        settings = read_json(directory / 'settings.json')
        evaluation_config = directory / 'evaluation-config.json'
        if evaluation_config.exists():
            settings['evaluation_config'] = str(evaluation_config)
        auth = self.worker_root / 'auth.json'
        child = Workflows(Store(directory / 'data'), settings,
                          auth_file=auth if auth.exists() else None)
        child.path = self.path
        history = directory / 'prior-observations.json'
        if phase == 'review' and history.exists():
            from .memory import Memory
            memory = Memory(child.store)
            for item in read_json(history):
                memory.append(item['model'], item['body'], item['sources'], item['event_key'])
        cancel = threading.Event()
        if (directory.parent / 'cancel').exists():
            cancel.set()
        finished = threading.Event()
        child.stop_event = cancel
        def watch():
            while not finished.wait(0.2):
                if self.stop_event.is_set() or (directory.parent / 'cancel').exists():
                    cancel.set()
                    return
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        active_marker = directory.parent / 'stage-active'
        active_marker.touch(mode=0o600, exist_ok=True)
        try:
            return child._measured_stage(job, phase, usage)
        except KeyboardInterrupt:
            return None
        finally:
            finished.set()
            watcher.join()
            active_marker.unlink(missing_ok=True)


def enqueue(root, job):
    coordinator = Coordinator(root)
    jid = identifier(job['id'])
    now = time.time()
    with coordinator.db() as db:
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (jid, job['kind'], identifier(job['task']), json.dumps(job['payload']),
                    'queued', 'queued', '', now, now, jid))


def start(root):
    root = Path(root).resolve()
    with (root / 'coordinator.log').open('ab') as log:
        subprocess.Popen([sys.executable, '-m', 'mineval.remote_coordinator', str(root)],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         close_fds=True, start_new_session=True)


def ensure_running(root):
    root = Path(root)
    try:
        with lock(root / 'coordinator.lock'):
            pass
    except ValueError as exc:
        if str(exc) == 'Task is busy in another preparation operation':
            return
        raise
    start(root)


def serve(root):
    root = Path(root).resolve()
    # A single owner loops over the durable queue; extra starts exit harmlessly.
    try:
        with lock(root / 'coordinator.lock'):
            coordinator = Coordinator(root)
            # A previous owner may have died after publishing stage state.
            for marker in (root / 'jobs').glob('*/stage-active'):
                marker.unlink(missing_ok=True)
            idle_since = time.monotonic()
            while True:
                coordinator.work()
                if any(job['state'] in ('queued', 'running') for job in coordinator.jobs()):
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since >= 30:
                    break
                time.sleep(1)
    except ValueError as exc:
        if str(exc) != 'Task is busy in another preparation operation':
            raise


if __name__ == '__main__':
    serve(sys.argv[1])
