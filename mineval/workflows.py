"""Historical workflow queue, shared by CLI and private human dashboard.

A single local worker lock spans execution. Durable checkpoints precede side effects;
uncertain work is interrupted, never automatically relaunched. Explicit retries keep
finished immutable artifacts and use new IDs for uncertain preparation/evaluation.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from . import benchmark, evaluation, review, service, verification
from .agents import CodexAdapter, FakeAdapter
from .artifacts import lock, read_json
from .categories import category_for
from .memory import encoded
from .sandbox import DockerSandbox

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
        # Host settings only. Never accepted as a browser request.
        self.settings = settings or {}
        self.context, self.auth_file = context, auth_file
        self.path = store.root / 'historical-jobs.sqlite3'
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS submissions(id TEXT PRIMARY KEY, created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS jobs(
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, task TEXT NOT NULL,
              payload TEXT NOT NULL, state TEXT NOT NULL, stage TEXT NOT NULL,
              detail TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
              dedup TEXT NOT NULL);
              CREATE UNIQUE INDEX IF NOT EXISTS active_job ON jobs(dedup)
                WHERE state IN ('queued','running','interrupted');''')

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

    def jobs(self):
        with self.db() as db:
            return [dict(r) | {'payload': json.loads(r['payload'])} for r in db.execute('SELECT * FROM jobs ORDER BY created DESC')]

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
                               'run_id': uuid4().hex, 'evaluation_id': uuid4().hex}
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
                p['run_id'] = uuid4().hex
                p['evaluation_id'] = uuid4().hex
            elif new_attempt:
                raise ValueError('Saved output is usable; retry the remaining stages')
            # Saved runs are reused; uncertain evaluator IDs are never overwritten.
            if not (self.store.task(row['task']) / 'evaluations' / (p['evaluation_id'] + '.json')).exists():
                p['evaluation_id'] = uuid4().hex
            db.execute("UPDATE jobs SET state='queued',detail='',payload=?,updated=? WHERE id=?", (encoded(p), time.time(), jid))

    def work(self, stop_event=None):
        self.stop_event = stop_event
        with lock(self.store.root / 'historical-worker.lock'):
            with self.db() as db:
                db.execute("UPDATE jobs SET state='interrupted',detail='Worker stopped during this stage. Inspect saved evidence, then retry explicitly.' WHERE state='running'")
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                with self.db() as db:
                    row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
                if row is None:
                    break
                jid = row['id']; p = json.loads(row['payload'])
                self.update(jid, state='running')
                try:
                    if row['kind'] == 'prepare':
                        self.prepare(jid, row['task'], p)
                    else:
                        self.benchmark(jid, row['task'], p)
                    self.update(jid, state='completed', stage='completed', detail='Finished; follow task links for immutable evidence')
                except KeyboardInterrupt:
                    self.update(jid, state='interrupted', detail='Interrupted; retry explicitly')
                    raise
                except Exception as exc:
                    # Raw agent/Docker exceptions may contain secrets. Only deliberate
                    # workflow guidance is exposed, never subprocess output.
                    detail = str(exc) if isinstance(exc, WorkflowFailure) else 'Stage failed (' + type(exc).__name__ + '). Inspect task evidence and trusted CLI configuration, then retry.'
                    self.update(jid, state='failed', detail=detail)

    def stage(self, jid, name):
        event = getattr(self, 'stop_event', None)
        if event is not None and event.is_set():
            raise KeyboardInterrupt()
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
        model = self.settings.get('preparation_model', 'gpt-6-luna')
        options = dict(adapter=CodexAdapter(model), auth_file=self.auth_file, context=self.context)
        result = service.session(self.store, task, **options)
        cleanup = read_json(self.store.task(task) / 'cleanup-latest.json')
        if cleanup.get('disposition') != 'ready-for-review':
            raise WorkflowFailure('Cleanup needs review. Inspect cleanup findings; correct prerequisites before retrying.')
        if all(c['name'] == 'bootstrap-runtime' for c in verification.load_config(config_path)['commands']):
            raise WorkflowFailure('Cleanup did not replace bootstrap diagnostics with repository checks; inspect cleanup and retry.')
        self.stage(jid, 'diagnostics')
        diagnostic = verification.diagnose(self.store, task, sandbox=DockerSandbox(context=self.context))
        if diagnostic.get('status') not in ('completed', 'checks_failed'):
            raise WorkflowFailure('Fresh diagnostics found an environment failure; inspect diagnostics and prerequisites.')
        self.stage(jid, 'private extraction')
        extraction = review.session(self.store, task, stage='extraction', **options)
        extraction_id = extraction.get('extraction_id')
        if not extraction_id:
            raise WorkflowFailure('No credible private verification was extracted; inspect extraction findings.')
        self.stage(jid, 'private validation')
        manifest = read_json(self.store.task(task) / 'private/extractions' / extraction_id / 'extraction-manifest.json')
        selected = self.settings.get('evaluation_configs', {}).get(task, self.settings.get('evaluation_config'))
        config = manifest.get('plan', {}).get('evaluation_config')
        if selected or config:
            import tempfile
            from .artifacts import write_json
            with tempfile.TemporaryDirectory(dir=self.store.root) as temp:
                path = Path(temp) / 'config.json'
                write_json(path, self.checked_config(verification.load_config(selected, evaluation=True) if selected else config))
                review.validate(self.store, task, extraction_id, path, sandbox=DockerSandbox(context=self.context))
                metadata = read_json(self.store.task(task) / 'metadata.json')
                future = metadata.get('head_commit')
                if future:
                    review.validate(self.store, task, extraction_id, path, future_commit=future, sandbox=DockerSandbox(context=self.context))
        self.stage(jid, 'independent review')
        approval = review.session(self.store, task, stage='review', extraction_id=extraction_id,
            **(options | {'adapter': CodexAdapter(self.settings.get('review_model', model))}))
        if approval['verdict'] != 'approve':
            raise WorkflowFailure('Independent review: ' + approval['verdict'] + '. ' + '; '.join(approval['reasons']))
        self.stage(jid, 'freezing')
        frozen = self.store.finalize(task, review_id=approval['review_id'])
        self.update(jid, payload=p | {'frozen': frozen})

    def benchmark(self, jid, task, p):
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
                    adapter=CodexAdapter(self.settings.get('evaluation_model', 'gpt-6-luna')) if self.settings.get('evaluator_agent', True) and p['configuration']['adapter'] != 'fake' else None,
                    auth_file=self.auth_file if self.settings.get('evaluator_agent', True) and p['configuration']['adapter'] != 'fake' else None)
                if assessed.get('error_kind') == 'interrupted':
                    raise KeyboardInterrupt()
        self.stage(jid, 'model review')
        from .memory import record_evaluation
        record_evaluation(self.store, task, assessed['evaluation_id'])
        if p['configuration']['adapter'] != 'fake' and self.settings.get('qualitative_review', True):
            from .model_review import review_evaluation, compact
            image = self.settings.get('review_image') or self.settings.get('preparation_image') or record.get('operation', {}).get('configured_image')
            if not image:
                raise WorkflowFailure('Qualitative review failed; benchmark and evaluation results are preserved. Configure review_image or preparation_image, then retry the review.')
            options = dict(image=image, context=self.context, auth_file=self.auth_file,
                           model=self.settings.get('review_model', 'gpt-6-luna'))
            try:
                review_evaluation(self.store, task, assessed['evaluation_id'], **options)
                from .memory import Memory
                while len(Memory(self.store).history(p['configuration']['model'])) >= Memory.threshold:
                    self.stage(jid, 'review compaction')
                    if compact(self.store, p['configuration']['model'], **options) is None:
                        break
            except Exception as exc:
                raise WorkflowFailure('Qualitative review failed (' + type(exc).__name__ + '); benchmark and evaluation results are preserved. Check the trusted review image and configuration, then retry the review.') from exc


class WorkflowFailure(ValueError):
    pass
