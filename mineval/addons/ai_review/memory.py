"""Transactional model review memory. Events and observations are never overwritten.

Only the active projection and expiring reservations mutate. A short SQLite write
transaction serializes claim/commit, never a model call. Model names are data, not paths.
"""
from contextlib import closing, contextmanager
import hashlib
import json
import sqlite3
import time
from uuid import uuid4


from mineval.core.artifacts import encoded

class Memory:
    threshold = 24

    def __init__(self, store):
        self.path = store.root / 'model-reviews.sqlite3'
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS observations (
              id TEXT PRIMARY KEY, model TEXT NOT NULL, sequence INTEGER NOT NULL, position INTEGER NOT NULL,
              body TEXT NOT NULL, sources TEXT NOT NULL, parents TEXT NOT NULL,
              active INTEGER NOT NULL, event_key TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS reservations (
              id TEXT PRIMARY KEY, model TEXT NOT NULL, inputs TEXT NOT NULL,
              expires REAL NOT NULL, state TEXT NOT NULL, result TEXT, submitted TEXT);
            CREATE TABLE IF NOT EXISTS events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
              created REAL NOT NULL, payload TEXT NOT NULL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            if 'position' not in {r['name'] for r in db.execute('PRAGMA table_info(observations)')}:
                db.execute('ALTER TABLE observations ADD COLUMN position INTEGER')
                db.execute('UPDATE observations SET position=sequence')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            yield db

    def event(self, db, kind, value):
        return db.execute('INSERT INTO events(kind,created,payload) VALUES(?,?,?)',
                          (kind, time.time(), encoded(value))).lastrowid

    @staticmethod
    def bullets(body):
        if (not isinstance(body, list) or not 1 <= len(body) <= 8 or
                any(not isinstance(x, str) or not x.strip() or len(x) > 1200 for x in body)):
            raise ValueError('Supply 1–8 concise, evidence-grounded observations (at most 1200 characters each)')
        return body

    def append(self, model, body, sources, event_key):
        self.bullets(body)
        if not sources or not isinstance(sources, list) or not model:
            raise ValueError('Model and evaluation sources are required')
        with self.transaction() as db:
            previous = db.execute('SELECT * FROM observations WHERE event_key=?', (event_key,)).fetchone()
            if previous:
                if previous['model'] != model or previous['sources'] != encoded(sources):
                    raise ValueError('Review idempotency key conflicts with existing sources')
                return self.decode(previous)
            oid = uuid4().hex
            seq = self.event(db, 'append', {'id': oid, 'model': model, 'sources': sources})
            db.execute('INSERT INTO observations(id,model,sequence,position,body,sources,parents,active,event_key) VALUES(?,?,?,?,?,?,?,1,?)',
                       (oid, model, seq, seq, encoded(body), encoded(sources), '[]', event_key))
            return self.decode(db.execute('SELECT * FROM observations WHERE id=?', (oid,)).fetchone())

    @staticmethod
    def decode(row):
        row = dict(row)
        for key in ('body', 'sources', 'parents'):
            row[key] = json.loads(row[key])
        return row

    @classmethod
    def history_rows(cls, db, model, archive=False):
        columns = {row['name'] for row in db.execute('PRAGMA table_info(observations)')}
        if not columns:
            return []
        order = 'sequence,id' if archive or 'position' not in columns else 'position,sequence,id'
        return [cls.decode(row) for row in db.execute(
            'SELECT * FROM observations WHERE model=?' + ('' if archive else ' AND active=1') +
            ' ORDER BY ' + order, (model,))]

    def history(self, model, *, archive=False):
        with self.connect() as db:
            return self.history_rows(db, model, archive)

    def claim(self, model, *, ttl=900, threshold=None):
        if not 1 <= ttl <= 3600:
            raise ValueError('Reservation TTL must be 1–3600 seconds')
        threshold = self.threshold if threshold is None else threshold
        if threshold < 2:
            raise ValueError('Compaction threshold must be at least two')
        with self.transaction() as db:
            db.execute("UPDATE reservations SET state='expired' WHERE state='claimed' AND expires<=?", (time.time(),))
            rows = list(db.execute('SELECT * FROM observations WHERE model=? AND active=1 ORDER BY position,sequence,id', (model,)))
            if len(rows) < threshold:
                return None
            ids = [r['id'] for r in rows[:2]]
            # Do not skip reserved oldest inputs: compaction always folds oldest two.
            for r in db.execute("SELECT inputs FROM reservations WHERE model=? AND state='claimed'", (model,)):
                if set(ids) & set(json.loads(r['inputs'])):
                    return None
            rid = uuid4().hex
            db.execute('INSERT INTO reservations VALUES(?,?,?,?,?,NULL,NULL)',
                       (rid, model, encoded(ids), time.time() + ttl, 'claimed'))
            self.event(db, 'claim', {'id': rid, 'inputs': ids, 'model': model})
            return {'reservation_id': rid, 'model': model, 'inputs': [self.decode(r) for r in rows[:2]]}

    def release(self, rid):
        """A failed inference releases its inputs immediately for an explicit retry."""
        with self.transaction() as db:
            changed = db.execute("UPDATE reservations SET state='failed' WHERE id=? AND state='claimed'", (rid,)).rowcount
            if changed:
                self.event(db, 'release', {'id': rid})

    def inspect_claim(self, rid):
        with self.connect() as db:
            r = db.execute('SELECT * FROM reservations WHERE id=?', (rid,)).fetchone()
            if not r:
                raise ValueError('Unknown reservation')
            return dict(r) | {'inputs': [self.decode(db.execute('SELECT * FROM observations WHERE id=?', (oid,)).fetchone())
                                        for oid in json.loads(r['inputs'])]}

    def commit(self, rid, body):
        self.bullets(body)
        submitted = hashlib.sha256(encoded(body).encode()).hexdigest()
        with self.transaction() as db:
            r = db.execute('SELECT * FROM reservations WHERE id=?', (rid,)).fetchone()
            if not r:
                raise ValueError('Unknown reservation')
            if r['state'] == 'committed':
                if submitted != r['submitted']:
                    raise ValueError('Reservation already committed with different content')
                return self.decode(db.execute('SELECT * FROM observations WHERE id=?', (r['result'],)).fetchone())
            if r['state'] != 'claimed' or r['expires'] <= time.time():
                raise ValueError('Reservation expired; claim again')
            parents = json.loads(r['inputs'])
            inputs = [db.execute('SELECT * FROM observations WHERE id=? AND active=1', (oid,)).fetchone() for oid in parents]
            if not all(inputs):
                raise ValueError('Compaction inputs are no longer active')
            sources = []
            for item in inputs:
                for source in json.loads(item['sources']):
                    if source not in sources:
                        sources.append(source)
            oid = uuid4().hex
            seq = self.event(db, 'compact', {'id': oid, 'reservation': rid, 'parents': parents})
            # Summary retains the oldest active position; its event sequence appends to the archive.
            db.execute('INSERT INTO observations(id,model,sequence,position,body,sources,parents,active,event_key) VALUES(?,?,?,?,?,?,?,1,?)',
                       (oid, r['model'], seq, min(x['position'] for x in inputs), encoded(body),
                        encoded(sources), encoded(parents), 'compact:' + rid))
            db.executemany('UPDATE observations SET active=0 WHERE id=?', [(x,) for x in parents])
            db.execute("UPDATE reservations SET state='committed',result=?,submitted=? WHERE id=?", (oid, submitted, rid))
            return self.decode(db.execute('SELECT * FROM observations WHERE id=?', (oid,)).fetchone())


def read_history(store, model, *, archive=False):
    """Read existing observations without creating or upgrading the store."""
    path = store.root / 'model-reviews.sqlite3'
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        return Memory.history_rows(db, model, archive)


def evaluation_source(store, task_id, evaluation_id):
    from mineval.core.artifacts import digest
    evaluation = store.inspect_evaluation(task_id, evaluation_id)
    if evaluation.get('schema_version') != 2:
        raise ValueError('Review requires a completed evaluation')
    run = store.inspect_run(task_id, evaluation['run_id'])
    return run['model']['name'], {
        'task_id': task_id, 'run_id': run['run_id'], 'evaluation_id': evaluation_id,
        'evaluation_sha256': digest(store.task(task_id) / 'evaluations' / (evaluation_id + '.json')),
        'prepared_revision': run['prepared_revision'], 'model': run['model'], 'harness': run['harness'],
        'verdict': evaluation['verdict']}, evaluation


def record_evaluation(store, task_id, evaluation_id, bullets=None):
    model, source, evidence = evaluation_source(store, task_id, evaluation_id)
    # Conservative fallback reports observed evidence only, never a personality inference.
    key = (f'qualitative-record:{task_id}:{evaluation_id}:' + hashlib.sha256(encoded(bullets).encode()).hexdigest()
           if bullets is not None else f'evaluation:{task_id}:{evaluation_id}')
    bullets = bullets if bullets is not None else [f"{task_id}: independent evaluation {evidence['verdict']}. " + evidence['explanation'][:900]]
    return Memory(store).append(model, bullets, [source], key)
