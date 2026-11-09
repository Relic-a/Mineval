"""Historical workflow queue, shared by CLI and private human dashboard.

A coordinator owns crash recovery; bounded stage workers execute independent jobs.
Durable checkpoints precede side effects;
uncertain work is interrupted, never automatically relaunched. Explicit retries keep
finished immutable artifacts and use new IDs for uncertain preparation/evaluation.
"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import os
import threading
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from . import benchmark, evaluation, review, service, verification
from .agents import CodexAdapter, FakeAdapter
from .artifacts import lock, read_json, write_json
from .categories import category_for
from .memory import encoded
from .sandbox import DockerSandbox, SandboxError

EFFORTS = ('minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')


def load_settings(store, filename=None):
    path = Path(filename).resolve(strict=True) if filename else store.root / 'historical-settings.json'
    settings = read_json(path) if path.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError('Historical settings must be a JSON object')
    settings = dict(settings)
    if settings.get('evaluation_config'):
        settings['evaluation_config'] = str((path.parent / settings['evaluation_config']).resolve(strict=True))
    settings['evaluation_configs'] = {task: str((path.parent / value).resolve(strict=True))
                                      for task, value in settings.get('evaluation_configs', {}).items()}
    for role in ('preparation', 'evaluation', 'review'):
        if role + '_model' in settings:
            configuration({'model': settings[role + '_model'], 'effort': settings.get(role + '_effort', 'medium')})
    for preset in settings.get('models', []):
        configuration(preset)
    return settings


def tasks(store):
    """Nonblocking pool inventory while long-running task operations hold locks.

    Immutable metadata and atomically published status are safe to read. Detail pages
    and actual launch still acquire task locks and validate complete receipts.
    """
    result = []
    for path in sorted((store.root / 'tasks').glob('*')):
        metadata = read_json(path / 'metadata.json')
        result.append({k: metadata[k] for k in ('task_id', 'repository', 'task_summary')} |
                      {'category': category_for(metadata),
                       'state': read_json(path / 'status.json'),
                       'versions': sorted(p.name for p in (path / 'versions').glob('*') if p.is_dir())})
    return result


def configuration(value):
    if not isinstance(value, dict) or set(value) - {'model', 'effort', 'adapter'}:
        raise ValueError('Model configuration requires model, effort and adapter')
    model = value.get('model', '')
    if not isinstance(model, str) or not model or model.startswith('-') or len(model) > 200 or any(c.isspace() for c in model):
        raise ValueError('Supply an explicit model identifier')
    effort = value.get('effort', 'medium')
    if effort not in EFFORTS or value.get('adapter', 'codex') not in ('codex', 'fake'):
        raise ValueError('Unsupported adapter or reasoning effort')
    return {'model': model, 'effort': effort, 'adapter': value.get('adapter', 'codex')}


def adapter(config):
    return (CodexAdapter(config['model'], effort=config['effort']) if config['adapter'] == 'codex'
            else FakeAdapter(model=config['model'], effort=config['effort']))


def matches(record, revision, config):
    return (record.get('schema_version') == 2 and record.get('prepared_revision') == revision
            and record.get('model') == {'name': config['model'], 'reasoning_effort': config['effort']}
            and record.get('harness') == {'name': config['adapter'], 'adapter_version': 1})


class Workflows:
    def __init__(self, store, settings=None, *, context=None, auth_file=None):
        self.store = store
        # Runtime paths and commands remain host-only; browser settings allow model choices only.
        self.settings = settings or {}
        self.context, self.auth_file = context, auth_file
        from .codex_models import Catalog
        self.catalog = Catalog(auth_file)
        self.path = store.root / 'historical-jobs.sqlite3'
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS submissions(id TEXT PRIMARY KEY, created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS jobs(
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, task TEXT NOT NULL,
              payload TEXT NOT NULL, state TEXT NOT NULL, stage TEXT NOT NULL,
              detail TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
              dedup TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS queued_job ON jobs(state,created);
              CREATE INDEX IF NOT EXISTS run_job ON jobs(task,kind,json_extract(payload,'$.run_id'));
              CREATE UNIQUE INDEX IF NOT EXISTS active_job ON jobs(dedup)
                WHERE state IN ('queued','running','interrupted');''')

    def model_settings(self):
        path = self.store.root / 'historical-model-settings.json'
        saved = read_json(path) if path.exists() else {}
        return {role: configuration(saved.get(role, {
            'model': self.settings.get(role + '_model', 'gpt-6-luna'),
            'effort': self.settings.get(role + '_effort', 'medium')}))
            for role in ('preparation', 'evaluation')}

    def save_model_settings(self, values):
        if set(values) != {'preparation', 'evaluation'}:
            raise ValueError('Choose preparation and evaluation models')
        checked = {role: configuration(value) for role, value in values.items()}
        if any(value['adapter'] != 'codex' for value in checked.values()):
            raise ValueError('Workflow models must use Codex')
        with lock(self.store.root / 'historical-model-settings.lock'):
            write_json(self.store.root / 'historical-model-settings.json', checked)

    def role_adapter(self, role, payload):
        choices = payload['model_settings'] if 'model_settings' in payload else self.model_settings()
        return adapter(choices[role])

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit_once(self, request_id):
        from .preparation import identifier
        identifier(request_id)
        with self.db() as db:
            return db.execute('INSERT OR IGNORE INTO submissions VALUES(?,?)', (request_id, time.time())).rowcount == 1

    def release_submission(self, request_id):
        # Validation failures have no queued side effects and may be corrected.
        with self.db() as db:
            db.execute('DELETE FROM submissions WHERE id=?', (request_id,))

    def jobs(self, *, state=None):
        with self.db() as db:
            query = 'SELECT * FROM jobs' + (' WHERE state=?' if state else '') + ' ORDER BY created DESC'
            return [dict(r) | {'payload': json.loads(r['payload'])} for r in db.execute(query, (state,) if state else ())]

    def update(self, jid, **changes):
        changes['updated'] = time.time()
        if 'payload' in changes:
            changes['payload'] = encoded(changes['payload'])
        with self.db() as db:
            db.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in changes) + ' WHERE id=?', (*changes.values(), jid))

    def eval_config(self, task, version=None):
        selected = self.settings.get('evaluation_configs', {}).get(task, self.settings.get('evaluation_config'))
        if selected:
            return verification.load_config(selected, evaluation=True)
        # A producer may supply a structured config; free-text plan instructions are
        # never interpreted as host commands or silently guessed.
        if version:
            frozen = self.store.task(task) / 'versions' / version
            manifest = read_json(frozen / 'private/verification/extraction-manifest.json')
            config = manifest.get('plan', {}).get('evaluation_config')
            if config:
                return self.checked_config(config)
        raise ValueError('Configure private evaluation commands in historical settings for this task; see workflow documentation')

    def checked_config(self, config):
        import tempfile
        from .artifacts import write_json
        with tempfile.TemporaryDirectory(dir=self.store.root) as temp:
            path = Path(temp) / 'config.json'; write_json(path, config)
            result = verification.load_config(path, evaluation=True)
        if not any(c['kind'] == 'tests' for c in result['commands']) or any(c.get('expected_failure') for c in result['commands']):
            raise ValueError('Evaluation requires private tests without expected_failure exemptions')
        return result

    def enqueue(self, kind, selections, configurations=None, *, exclude=True):
        if kind not in ('prepare', 'benchmark') or not isinstance(selections, list):
            raise ValueError('Select preparation or benchmark tasks')
        if len(selections) > 1000:
            raise ValueError('Select at most 1000 tasks per request')
        configs = [configuration(c) for c in (configurations or [])]
        if kind == 'benchmark' and not 1 <= len(configs) <= 50:
            raise ValueError('Select 1–50 model configurations')
        model_settings = self.model_settings()
        pool = {t['task_id']: t for t in tasks(self.store)}
        results = []
        for selection in selections:
            task = selection['task_id']
            try:
                t = pool[task]
                if t['state']['status'] in ('retired', 'rejected', 'needs-review'):
                    raise ValueError('Task is not eligible; inspect task state')
                if kind == 'prepare' and t['versions']:
                    results.append({'task': task, 'state': 'skipped', 'detail': 'Already prepared'})
                    continue
                revision = self.store._inspect_revision(task, selection['version']) if kind == 'benchmark' else None
                for config in configs if kind == 'benchmark' else [None]:
                    payload = {'selection': selection, 'revision': revision, 'configuration': config,
                               'run_id': uuid4().hex, 'evaluation_id': uuid4().hex,
                               'model_settings': model_settings}
                    if kind == 'benchmark':
                        payload['evaluation_config'] = self.checked_config(self.eval_config(task, selection['version']))
                    dedup = hashlib.sha256(encoded([kind, task, revision, config]).encode()).hexdigest()
                    # Serialize repeated selection across HTTP threads/processes. Task
                    # launch uses the same immutable revision checked above.
                    with self.db() as db:
                        db.execute('BEGIN IMMEDIATE')
                        existing = db.execute("SELECT id FROM jobs WHERE dedup=? AND state IN ('queued','running','interrupted')", (dedup,)).fetchone()
                        previous = []
                        if kind == 'benchmark' and exclude and not existing:
                            previous = [benchmark.inspect_attempt(self.store, task, path.stem)
                                        for path in (self.store.task(task) / 'runs').glob('*.json')]
                        if existing:
                            results.append({'task': task, 'state': 'existing', 'id': existing['id']})
                        elif any(matches(r, revision, config) for r in previous):
                            results.append({'task': task, 'state': 'skipped', 'detail': 'Exact configuration and revision already attempted; disable exclusion to rerun'})
                        else:
                            jid = uuid4().hex
                            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                                (jid, kind, task, encoded(payload), 'queued', 'queued', '', time.time(), time.time(), dedup))
                            results.append({'task': task, 'state': 'queued', 'id': jid})
            except (ValueError, OSError, KeyError, TypeError) as exc:
                results.append({'task': task, 'state': 'error', 'detail': str(exc)})
        return results

    def needs_new_attempt(self, job):
        if job['kind'] != 'benchmark':
            return False
        path = self.store.task(job['task']) / 'runs' / (job['payload']['run_id'] + '.json')
        if not path.exists():
            return False
        record = self.store.inspect_run(job['task'], job['payload']['run_id'])
        return record['status'] == 'running' or not record.get('output_available', False)

    def retry(self, jid, *, new_attempt=False):
        # Failed/interrupted states are published only after ownership ended; the
        # atomic state transition permits retries while unrelated work is running.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
            if not row or row['state'] not in ('failed', 'interrupted'):
                raise ValueError('Only failed or interrupted work may be retried')
            other = db.execute("SELECT id FROM jobs WHERE dedup=? AND id!=? AND state IN ('queued','running','interrupted')", (row['dedup'], jid)).fetchone()
            if other:
                raise ValueError('This configuration already has active work; open the queue instead')
            p = json.loads(row['payload'])
            unusable = self.needs_new_attempt(dict(row) | {'payload': p})
            if unusable:
                record = self.store.inspect_run(row['task'], p['run_id'])
                if record['status'] == 'running':
                    benchmark.recover(self.store, row['task'], p['run_id'])
                if not new_attempt:
                    raise ValueError('This attempt has no usable saved output; start a new benchmark attempt')
                p['phase'] = 'benchmark'
                p['run_id'] = uuid4().hex
                p['evaluation_id'] = uuid4().hex
            elif new_attempt:
                raise ValueError('Saved output is usable; retry the remaining stages')
            # Interrupted evaluations remain immutable evidence, not reusable verdicts.
            evaluation_path = self.store.task(row['task']) / 'evaluations' / (p['evaluation_id'] + '.json')
            incomplete = evaluation_path.exists() and not evaluation_path.with_suffix('.sha256').exists()
            if incomplete or (evaluation_path.exists() and self.store.inspect_evaluation(row['task'], p['evaluation_id']).get('error_kind') == 'interrupted'):
                p['phase'] = 'evaluation'
                p['internal_evaluation_ids'] = p.get('internal_evaluation_ids', []) + [p['evaluation_id']]
                p['evaluation_id'] = uuid4().hex
            elif not evaluation_path.exists():
                p['internal_evaluation_ids'] = p.get('internal_evaluation_ids', []) + [p['evaluation_id']]
                if p.get('phase') == 'review':
                    p['phase'] = 'evaluation'
                p['evaluation_id'] = uuid4().hex
            db.execute("UPDATE jobs SET state='queued',detail='',payload=?,updated=? WHERE id=?", (encoded(p), time.time(), jid))

    def schedule_compaction(self, model, options):
        # One durable request per model. Authentication stays in the worker, never the DB.
        safe = {k: v for k, v in options.items() if k != 'auth_file'}
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS maintenance(model TEXT PRIMARY KEY, options TEXT NOT NULL)")
            db.execute("INSERT INTO maintenance VALUES(?,?) ON CONFLICT(model) DO UPDATE SET options=excluded.options",
                       (model, encoded(safe)))

    def maintain(self):
        from .model_review import compact
        from .memory import Memory
        with lock(self.store.root / 'maintenance-worker.lock'):
            with self.db() as db:
                db.execute("CREATE TABLE IF NOT EXISTS maintenance(model TEXT PRIMARY KEY, options TEXT NOT NULL)")
                requests = list(db.execute('SELECT * FROM maintenance'))
            for request in requests:
                options = json.loads(request['options']) | {'auth_file': self.auth_file}
                while len(Memory(self.store).history(request['model'])) >= Memory.threshold:
                    if compact(self.store, request['model'], **options) is None:
                        break
                # Keep requests: future appends and expired reservations need no re-enqueue.

    def resource_request(self, job, phase):
        from .resources import options
        values = options(self.settings)
        # Estimates are independent of Docker ceilings, including tmpfs (which is
        # charged inside the container's memory limit).
        count = 2 if phase == 'prepare' else 1  # Agent plus broker verification.
        return values['cpus_per_job'], count * values['memory_mb_per_job']

    def concurrency(self):
        values = {'prepare': 1, 'benchmark': 2, 'evaluation': 1, 'review': 1,
                  'total': min(4, max(1, (os.cpu_count() or 2) // 2)), 'per_model': 2}
        supplied = self.settings.get('concurrency', {})
        if not isinstance(supplied, dict) or set(supplied) - values.keys():
            raise ValueError('Unknown concurrency setting')
        values.update(supplied)
        if any(type(v) is not int or not 1 <= v <= 64 for v in values.values()):
            raise ValueError('Concurrency limits must be integers between 1 and 64')
        providers = self.settings.get('model_concurrency', {})
        if not isinstance(providers, dict) or any(type(v) is not int or not 1 <= v <= 64 for v in providers.values()):
            raise ValueError('Model concurrency limits must be integers between 1 and 64')
        return values

    def work(self, stop_event=None):
        # One coordinator may own a store. This lock is never used by task work;
        # recovery cannot mistake another live coordinator's jobs for crashed work.
        self.stop_event = stop_event or threading.Event()
        limits = self.concurrency()
        from .resources import options, admits, Usage
        options(self.settings)  # Validate resource settings before claiming work.
        interrupted = False
        with lock(self.store.root / 'historical-worker.lock'):
            with self.db() as db:
                db.execute("UPDATE jobs SET state='interrupted',detail='Worker stopped during this stage. Inspect saved evidence, then retry explicitly.' WHERE state='running'")
            with ThreadPoolExecutor(max_workers=limits['total'], thread_name_prefix='mineval') as pool:
                active = {}
                last_start = float("-inf")
                try:
                    while True:
                        if not self.stop_event.is_set():
                            for job in reversed(self.jobs(state='queued')):
                                if job['state'] != 'queued' or len(active) >= limits['total']:
                                    continue
                                phase = 'prepare' if job['kind'] == 'prepare' else job['payload'].get('phase', 'benchmark')
                                cpu, memory = self.resource_request(job, phase)
                                # Soft estimates control parallelism, never all preparation
                                # progress. Runtime checks and hard limits still apply.
                                first_preparation = phase == 'prepare' and not active
                                if not first_preparation and not admits(self.settings, [(c, r, usage) for p, m, c, r, usage in active.values()],
                                              (cpu, memory), now=time.monotonic(), last_start=last_start):
                                    continue
                                choices = job['payload'].get('model_settings', {})
                                role = 'preparation' if phase == 'prepare' else 'evaluation'
                                model = ((job['payload'].get('configuration') or {}).get('model') if phase == 'benchmark' else choices.get(role, {}).get('model'))
                                if phase == 'review':
                                    model = self.settings.get('review_model', model)
                                models = {model} if model else set()
                                if phase == 'prepare' and self.settings.get('review_model'):
                                    models.add(self.settings['review_model'])
                                if sum(p == phase for p, m, c, r, usage in active.values()) >= limits[phase]:
                                    continue
                                if any(sum(model in m for p, m, c, r, usage in active.values()) >= self.settings.get('model_concurrency', {}).get(model, limits['per_model']) for model in models):
                                    continue
                                with self.db() as db:
                                    claimed = db.execute("UPDATE jobs SET state='running',updated=? WHERE id=? AND state='queued'", (time.time(), job['id'])).rowcount
                                if claimed:
                                    usage = Usage(expected_containers=2 if phase == 'prepare' else 1, values=options(self.settings))
                                    active[pool.submit(self._measured_stage, job, phase, usage)] = (phase, models, cpu, memory, usage)
                                    last_start = time.monotonic()
                                    break  # Observe a fresh interval before another admission.
                        if not active:
                            delay = options(self.settings)['admission_interval_seconds'] - (time.monotonic() - last_start)
                            if delay > 0 and not self.stop_event.is_set() and self.jobs(state='queued'):
                                self.stop_event.wait(min(delay, 0.2))
                                continue
                            break
                        delay = options(self.settings)['admission_interval_seconds'] - (time.monotonic() - last_start)
                        done, _ = wait(active, timeout=min(0.2, delay) if delay > 0 else 0.2, return_when=FIRST_COMPLETED)
                        for future in done:
                            del active[future]
                            try:
                                future.result()
                            except KeyboardInterrupt:
                                interrupted = True
                                self.stop_event.set()
                except KeyboardInterrupt:
                    interrupted = True
                    self.stop_event.set()
                # Running containers own their cleanup. Do not release coordinator
                # ownership until their bounded lifecycles have ended.
                for future in active:
                    try:
                        future.result()
                    except KeyboardInterrupt:
                        interrupted = True
            if interrupted:
                raise KeyboardInterrupt()

    def _measured_stage(self, job, phase, usage):
        from .resources import Usage
        Usage.local.current = usage
        started = time.monotonic()
        try:
            return self._work_stage(job, phase)
        finally:
            Usage.local.current = None
            # Operational observations stay outside immutable benchmark receipts.
            # A telemetry filesystem failure must not replace the stage outcome.
            try:
                directory = self.store.root / 'resource-usage'
                directory.mkdir(exist_ok=True)
                write_json(directory / (job['id'] + '-' + phase + '-' + uuid4().hex + '.json'),
                           {'job_id': job['id'], 'phase': phase, 'duration_seconds': time.monotonic() - started,
                            'containers': usage.snapshot()})
            except OSError:
                pass

    def _work_stage(self, job, phase):
        jid, task, p = job['id'], job['task'], job['payload']
        from .sandbox import DockerSandbox as Runtime
        Runtime._cancellation.event = self.stop_event
        try:
            if phase == 'prepare':
                self.prepare(jid, task, p)
            else:
                self.benchmark(jid, task, p, phase=phase)
                following = {'benchmark': 'evaluation', 'evaluation': 'review'}.get(phase)
                if following:
                    self.stage(jid, following)
                    self.update(jid, state='queued', payload=p | {'phase': following})
                    return
            self.update(jid, state='completed', stage='completed', detail='Finished; follow task links for immutable evidence')
        except KeyboardInterrupt:
            self.update(jid, state='interrupted', detail='Interrupted; retry explicitly')
            raise
        except Exception as exc:
            from .devlog import failure
            failure("workflow.failed", exc, job_id=jid)
            detail = str(exc) if isinstance(exc, WorkflowFailure) else 'Stage failed (' + type(exc).__name__ + '). Inspect task evidence and trusted CLI configuration, then retry.'
            if isinstance(exc, (SandboxError, MemoryError)):
                detail = 'Infrastructure failure (' + type(exc).__name__ + '): runtime execution failed. Inspect Docker resource availability, isolation requirements and task evidence, then retry.'
            self.update(jid, state='failed', detail=detail)
        finally:
            Runtime._cancellation.event = None

    def stage(self, jid, name):
        event = getattr(self, 'stop_event', None)
        if event is not None and event.is_set():
            raise KeyboardInterrupt()
        from .devlog import event
        event("workflow.stage", job_id=jid, stage=name)
        self.update(jid, stage=name)

    def prepare(self, jid, task, p):
        state = read_json(self.store.task(task) / 'status.json')
        if state['status'] == 'frozen':
            self.store.inspect_revision(task, state['version'])
            return
        self.stage(jid, 'runtime setup')
        config_path = self.store.draft(task) / 'control/verify.json'
        with self.store.operation(task):
            current = read_json(config_path)
            if not current.get('image') or not current.get('commands'):
                image = self.settings.get('preparation_image')
                if not image:
                    raise WorkflowFailure('Choose a trusted preloaded preparation image with --image or preparation_image in historical settings, then retry.')
                from .artifacts import write_json
                write_json(config_path, {'image': image, 'network': 'bridge',
                    'limits': {'timeout_seconds': 600}, 'commands': [
                    {'name': 'bootstrap-runtime', 'kind': 'setup', 'argv': ['python3', '--version']}]})
        self.stage(jid, 'cleanup')
        prep_adapter = self.role_adapter('preparation', p)
        options = dict(adapter=prep_adapter, auth_file=self.auth_file, context=self.context)
        from .checkpoints import PreparationCheckpoints
        runtime = DockerSandbox(context=self.context)
        progress = PreparationCheckpoints(self.store, task,
            lambda image: runtime.image(image)['Id'] if hasattr(runtime, 'image') else image)
        def cleanup_key():
            from .cleanup import INSTRUCTIONS
            return [progress.state(), p.get('model_settings', {}).get('preparation'), INSTRUCTIONS]
        def clean():
            service.session(self.store, task, **options)
            return read_json(self.store.task(task) / 'cleanup-latest.json')
        cleanup = progress.run('cleanup', cleanup_key(), clean, progress.cleanup, post_inputs=cleanup_key)
        if cleanup.get('disposition') != 'ready-for-review':
            raise WorkflowFailure('Cleanup needs review. Inspect cleanup findings; correct prerequisites before retrying.')
        if all(c['name'] == 'bootstrap-runtime' for c in verification.load_config(config_path)['commands']):
            raise WorkflowFailure('Cleanup did not replace bootstrap diagnostics with repository checks; inspect cleanup and retry.')
        self.stage(jid, 'diagnostics')
        diagnostic = progress.run('diagnostics', progress.state(),
            lambda: progress.current_diagnostic() or verification.diagnose(self.store, task, sandbox=DockerSandbox(context=self.context)), progress.diagnostic)
        if diagnostic.get('status') not in ('completed', 'checks_failed'):
            raise WorkflowFailure('Fresh diagnostics found an environment failure; inspect diagnostics and prerequisites.')
        self.stage(jid, 'private extraction')
        selected = self.settings.get('evaluation_configs', {}).get(task, self.settings.get('evaluation_config'))
        def reusable_extraction(value):
            if not progress.extraction(value):
                return False
            manifest = read_json(self.store.task(task) / 'private/extractions' / value['extraction_id'] / 'extraction-manifest.json')
            return bool(selected or manifest.get('plan', {}).get('evaluation_config'))
        revision_id = progress.revision()
        extraction = progress.run('extraction', [progress.state(), cleanup, diagnostic, p.get('model_settings', {}).get('preparation'), review.EXTRACTION, revision_id],
            lambda: review.session(self.store, task, stage='extraction', revision_id=revision_id,
                require_evaluation_config=not bool(selected), **options), reusable_extraction)
        extraction_id = extraction.get('extraction_id')
        if not extraction_id:
            raise WorkflowFailure('No credible private verification was extracted; inspect extraction findings.')
        self.stage(jid, 'private validation')
        manifest = read_json(self.store.task(task) / 'private/extractions' / extraction_id / 'extraction-manifest.json')
        config = manifest.get('plan', {}).get('evaluation_config')
        if not (selected or config):
            raise WorkflowFailure('Private extraction supplied no runnable evaluation configuration; '
                                  'provide a trusted private configuration or retry extraction with runnable checks.')
        if selected or config:
            import tempfile
            from .artifacts import write_json
            with tempfile.TemporaryDirectory(dir=self.store.root) as temp:
                path = Path(temp) / 'config.json'
                write_json(path, self.checked_config(verification.load_config(selected, evaluation=True) if selected else config))
                validation_key = [review.VALIDATION_POLICY, progress.state(), extraction_id, review.checked(self.store.task(task) / 'private/extractions' / extraction_id), read_json(path), progress.runtime(read_json(path)['image'])]
                progress.run('validation-base', validation_key,
                    lambda: review.validate(self.store, task, extraction_id, path, sandbox=DockerSandbox(context=self.context)), progress.validation)
                metadata = read_json(self.store.task(task) / 'metadata.json')
                future = metadata.get('head_commit')
                if future:
                    progress.run('validation-future', [validation_key, future],
                        lambda: review.validate(self.store, task, extraction_id, path, future_commit=future, sandbox=DockerSandbox(context=self.context)), progress.validation)
        self.stage(jid, 'independent review')
        approval = progress.run('review', [review.inputs(self.store, task, extraction_id), progress.state(),
            p.get('model_settings', {}).get('preparation'), self.settings.get('review_model'), self.settings.get('review_effort'), review.REVIEW],
            lambda: review.session(self.store, task, stage='review', extraction_id=extraction_id,
                **(options | {'adapter': CodexAdapter(self.settings['review_model'], effort=self.settings.get('review_effort')) if self.settings.get('review_model') else prep_adapter})), progress.approval)
        if approval['verdict'] != 'approve':
            raise WorkflowFailure('Independent review: ' + approval['verdict'] + '. ' + '; '.join(approval['reasons']))
        self.stage(jid, 'freezing')
        frozen = self.store.finalize(task, review_id=approval['review_id'])
        self.update(jid, payload=p | {'frozen': frozen})

    def benchmark(self, jid, task, p, *, phase=None):
        if phase in (None, 'benchmark'):
            self.stage(jid, 'benchmark')
            path = self.store.task(task) / 'runs' / (p['run_id'] + '.json')
            if path.exists():
                record = self.store.inspect_run(task, p['run_id'])
                if not matches(record, p['revision'], p['configuration']):
                    raise WorkflowFailure('Saved run identity does not match queued work')
                if record['status'] == 'running':
                    benchmark.recover(self.store, task, p['run_id'])
                    raise WorkflowFailure('Interrupted attempt sealed as an infrastructure error. Explicitly rerun for a new attempt; its evidence is preserved.')
            else:
                record = benchmark.run(self.store, task, p['selection']['version'], run_id=p['run_id'],
                    adapter=adapter(p['configuration']), context=self.context,
                    auth_file=self.auth_file if p['configuration']['adapter'] == 'codex' else None)
                if record['status'] == 'cancelled':
                    raise KeyboardInterrupt()
            if not record.get('output_available', False):
                raise WorkflowFailure('Attempt has no usable saved output. Start a new benchmark attempt; its evidence is preserved.')
            if phase == 'benchmark':
                return
        else:
            record = self.store.inspect_run(task, p['run_id'])
        if phase in (None, 'evaluation'):
            self.stage(jid, 'evaluation')
            ep = self.store.task(task) / 'evaluations' / (p['evaluation_id'] + '.json')
            if ep.exists():
                assessed = self.store.inspect_evaluation(task, p['evaluation_id'])
            else:
                import tempfile
                from .artifacts import write_json
                with tempfile.TemporaryDirectory(dir=self.store.root) as temp:
                    config = Path(temp) / 'config.json'; write_json(config, p['evaluation_config'])
                    assessed = evaluation.evaluate(self.store, task, p['run_id'], config,
                        evaluation_id=p['evaluation_id'], sandbox=DockerSandbox(context=self.context),
                        adapter=self.role_adapter('evaluation', p) if self.settings.get('evaluator_agent', True) and p['configuration']['adapter'] != 'fake' else None,
                        auth_file=self.auth_file if self.settings.get('evaluator_agent', True) and p['configuration']['adapter'] != 'fake' else None,
                        evaluator_timeout=self.settings.get('evaluator_timeout_seconds', 180))
            if assessed.get('error_kind') == 'interrupted':
                raise KeyboardInterrupt()
            if phase == 'evaluation':
                return
        else:
            assessed = self.store.inspect_evaluation(task, p['evaluation_id'])
        self.stage(jid, 'model review')
        from .memory import record_evaluation
        record_evaluation(self.store, task, assessed['evaluation_id'])
        if p['configuration']['adapter'] != 'fake' and self.settings.get('qualitative_review', True):
            from .model_review import review_evaluation
            image = self.settings.get('review_image') or self.settings.get('preparation_image') or record.get('operation', {}).get('configured_image')
            if not image:
                raise WorkflowFailure('Qualitative review failed; benchmark and evaluation results are preserved. Configure review_image or preparation_image, then retry the review.')
            options = dict(image=image, context=self.context, auth_file=self.auth_file,
                           model=self.settings.get('review_model', self.role_adapter('evaluation', p).model),
                           effort=self.settings.get('review_effort', self.role_adapter('evaluation', p).effort))
            try:
                review_evaluation(self.store, task, assessed['evaluation_id'], **options)
                self.schedule_compaction(p['configuration']['model'], options)
            except Exception as exc:
                raise WorkflowFailure('Qualitative review failed (' + type(exc).__name__ + '); benchmark and evaluation results are preserved. Check the trusted review image and configuration, then retry the review.') from exc


class WorkflowFailure(ValueError):
    pass
