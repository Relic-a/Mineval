"""Web Arena: independent tasks, immutable generations and append-only preferences."""
from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import hashlib
import itertools
import json
import math
from pathlib import Path
import shutil
import sqlite3
import tempfile
from uuid import uuid4

from .artifacts import copy_tree, digest, inventory, pack, unpack, write_json

EFFORTS = ('minimal', 'low', 'medium', 'high', 'xhigh')


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value, name, limit=16000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\0' in value:
        raise ValueError(f'{name} must contain 1–{limit} characters')
    return value.strip()


class Arena:
    def __init__(self, root):
        self.root = Path(root).resolve() / 'arena'
        self.root.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
CREATE TABLE IF NOT EXISTS benchmarks(id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS generations(id TEXT PRIMARY KEY, benchmark TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS judgments(id TEXT PRIMARY KEY, benchmark TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exports(id TEXT PRIMARY KEY, benchmark TEXT NOT NULL, data TEXT NOT NULL);
''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / 'arena.sqlite3', timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def records(self, table, benchmark=None):
        if table not in ('benchmarks', 'generations', 'judgments', 'exports'):
            raise ValueError('Unknown Arena record')
        with self.db() as db:
            rows = db.execute('SELECT data FROM ' + table + (' WHERE benchmark=?' if benchmark else '') + ' ORDER BY rowid', (benchmark,) if benchmark else ()).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get(self, table, key):
        return next((r for r in self.records(table) if r['id'] == key), None) or self.missing()

    @staticmethod
    def missing():
        raise ValueError('Arena record does not exist')

    def insert(self, table, record):
        with self.db() as db:
            if table == 'benchmarks':
                db.execute('INSERT INTO benchmarks VALUES (?,?)', (record['id'], json.dumps(record)))
            else:
                db.execute('INSERT INTO ' + table + ' VALUES (?,?,?)', (record['id'], record['benchmark'], json.dumps(record)))
        return record

    def create(self, data):
        title = text(data.get('title'), 'Title', 160)
        prompt = text(data.get('prompt'), 'Prompt', 64000)
        references = data.get('references', '')
        if not isinstance(references, str) or len(references) > 16000:
            raise ValueError('References must be text up to 16000 characters')
        key = uuid4().hex
        target = self.root / 'inputs' / key
        target.mkdir(parents=True)
        try:
            assets = data.get('assets', [])
            if not isinstance(assets, list) or len(assets) > 20:
                raise ValueError('At most 20 assets')
            total = 0
            for asset in assets:
                name = text(asset['name'], 'Asset filename', 160)
                from .artifacts import relative
                relative(name)
                if '/' in name:
                    raise ValueError('Assets must have simple filenames')
                raw = base64.b64decode(asset['data'], validate=True)
                total += len(raw)
                if total > 20 * 1024 * 1024:
                    raise ValueError('Assets exceed 20 MiB')
                (target / 'assets').mkdir(exist_ok=True)
                with (target / 'assets' / name).open('xb') as f:
                    f.write(raw)
            if data.get('starter'):
                raw = base64.b64decode(data['starter'], validate=True)
                if len(raw) > 32 * 1024 * 1024:
                    raise ValueError('Starter archive exceeds 32 MiB')
                archive = target / 'starter.tar.gz'
                archive.write_bytes(raw)
                unpack(archive, target / 'starter')
                archive.unlink()
            receipt = inventory(target)
            return self.insert('benchmarks', dict(id=key, title=title, prompt=prompt,
                references=references, created_at=now(), inputs=receipt, private=bool(data.get('private', True))))
        except BaseException:
            shutil.rmtree(target)
            raise

    def check_inputs(self, benchmark):
        root = self.root / 'inputs' / benchmark['id']
        if inventory(root) != benchmark['inputs']:
            raise ValueError('Benchmark input hash mismatch')
        return root

    def enqueue(self, benchmark, configs):
        task = self.get('benchmarks', benchmark)
        self.check_inputs(task)
        if not isinstance(configs, list) or not 1 <= len(configs) <= 24:
            raise ValueError('Choose 1–24 configurations')
        validated = []
        for cfg in configs:
            harness = cfg.get('harness', 'codex')
            if harness not in ('codex', 'fixture'):
                raise ValueError('Unsupported harness')
            model = text(cfg.get('model'), 'Model', 120)
            if model.startswith('-') or any(c.isspace() for c in model):
                raise ValueError('Model must be a single identifier')
            effort = cfg.get('effort', 'medium')
            if effort not in EFFORTS:
                raise ValueError('Unsupported reasoning effort')
            validated.append(dict(harness=harness, model=model, effort=effort))
        result = []
        with self.db() as db:
            for cfg in validated:
                record = dict(id=uuid4().hex, benchmark=benchmark, config=cfg,
                              created_at=now(), status='queued')
                db.execute('INSERT INTO generations VALUES (?,?,?)', (record['id'], benchmark, json.dumps(record)))
                result.append(record)
        return result

    def transition(self, key, expected, **updates):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            record = json.loads(db.execute('SELECT data FROM generations WHERE id=?', (key,)).fetchone()[0])
            if record['status'] != expected:
                raise ValueError('Generation state changed')
            record.update(updates)
            db.execute('UPDATE generations SET data=? WHERE id=?', (json.dumps(record), key))
        return record

    def publish(self, key, source, **metadata):
        record = self.get('generations', key)
        target = self.root / 'outputs' / key
        target.mkdir(parents=True, exist_ok=False)
        pack(source, target / 'website.tar.gz')
        return self.transition(key, 'running', status='ready', finished_at=now(),
            artifact_sha256=digest(target / 'website.tar.gz'), **metadata)

    def artifact(self, key):
        record = self.get('generations', key)
        if record['status'] != 'ready':
            raise ValueError('Website is not ready')
        archive = self.root / 'outputs' / key / 'website.tar.gz'
        if digest(archive) != record['artifact_sha256']:
            raise ValueError('Website artifact hash mismatch')
        return archive

    def judge(self, benchmark, left, right, preference, note=''):
        if left == right or preference not in ('left', 'right', 'tie'):
            raise ValueError('Choose two different websites and a preference')
        for key in (left, right):
            r = self.get('generations', key)
            if r['benchmark'] != benchmark or r['status'] != 'ready':
                raise ValueError('Judgments require ready websites from this benchmark')
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError('Judgment note is too long')
        # One initial judgment per unordered pair, transactionally. An explicit revision
        # would be a future append-only operation; silent replacements are disallowed.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT data FROM judgments WHERE benchmark=?', (benchmark,)).fetchall()
            if any({json.loads(r[0])['left'], json.loads(r[0])['right']} == {left, right} for r in rows):
                raise ValueError('This pair already has a saved judgment')
            record = dict(id=uuid4().hex, benchmark=benchmark, left=left, right=right,
                          preference=preference, note=note, created_at=now())
            db.execute('INSERT INTO judgments VALUES (?,?,?)', (record['id'], benchmark, json.dumps(record)))
        return record

    def leaderboard(self, benchmark, group='runs'):
        runs = [r for r in self.records('generations', benchmark) if r['status'] == 'ready']
        votes = self.records('judgments', benchmark)
        scores = {r['id']: 0.0 for r in runs}
        counts = dict.fromkeys(scores, 0)
        # Regularized Bradley–Terry fit. Ties are half-wins. A Gaussian prior keeps
        # disconnected/sparse components finite; uncertainty is approximate, not a CI.
        for v in votes:
            counts[v['left']] += 1; counts[v['right']] += 1
        for _ in range(100):
            gradients = {k: -s for k, s in scores.items()}
            precision = dict.fromkeys(scores, 1.0)
            for v in votes:
                a, b = v['left'], v['right']
                p = 1 / (1 + math.exp(max(-30, min(30, scores[b] - scores[a]))))
                y = 1 if v['preference'] == 'left' else 0 if v['preference'] == 'right' else .5
                gradients[a] += y - p; gradients[b] -= y - p
                precision[a] += p * (1-p); precision[b] += p * (1-p)
            for k in scores:
                scores[k] += .5 * gradients[k] / precision[k]
        rows = [dict(r, score=scores[r['id']], uncertainty=1 / math.sqrt(precision[r['id']]),
                     judgments=counts[r['id']]) for r in runs]
        if group not in ('runs', 'configuration', 'model'):
            raise ValueError('Unknown grouping')
        if group != 'runs':
            groups = {}
            for r in rows:
                c = r['config']
                key = (c['harness'], c['model'], c['effort'] if group == 'configuration' else '')
                groups.setdefault(key, []).append(r)
            rows = [dict(members[0], id=' / '.join(key), members=members,
                         score=sum(x['score'] for x in members)/len(members),
                         uncertainty=sum(x['uncertainty'] for x in members)/len(members),
                         judgments=sum(x['judgments'] for x in members)) for key, members in groups.items()]
        rows = sorted(rows, key=lambda r: (-r['score'], r['created_at'], r['id']))
        for i, row in enumerate(rows):
            row['position'] = (next(j+1 for j, other in enumerate(rows) if abs(other['score']-row['score']) < 1e-6)
                               if row['judgments'] else None)
        return rows

    def comparison(self, benchmark):
        rows = self.leaderboard(benchmark)
        votes = self.records('judgments', benchmark)
        seen = {frozenset((v['left'], v['right'])) for v in votes}
        # A bounded recommendation budget avoids exhaustively comparing a large
        # tie cluster. Later generations still receive placement comparisons.
        budget = math.ceil(len(rows) * math.log2(max(2, len(rows))))
        if rows and len(votes) >= budget and all(r['judgments'] >= 2 for r in rows):
            return None
        choices = []
        for a, b in itertools.combinations(rows, 2):
            if frozenset((a['id'], b['id'])) in seen:
                continue
            # New entries first face a central established anchor, then nearby/uncertain
            # neighbors. Connected informative pairs outrank wholly unobserved pairs.
            variance = a['uncertainty'] ** 2 + b['uncertainty'] ** 2
            information = variance / (1 + abs(a['score'] - b['score']))
            if bool(a['judgments']) != bool(b['judgments']):
                information += 2
            # Stop recommending distant established pairs. Adjacent uncertain
            # positions remain eligible, allowing cycles/ties rather than imposing
            # binary-search transitivity on human preferences.
            if a['judgments'] >= 2 and b['judgments'] >= 2 and abs(a['score'] - b['score']) > 1.25 * math.sqrt(variance):
                continue
            choices.append((information, a['id'], b['id']))
        if not choices:
            return None
        _, a, b = max(choices)
        return dict(left=a, right=b)

    def summary(self, benchmark, group='runs'):
        return dict(benchmark=self.get('benchmarks', benchmark),
            generations=self.records('generations', benchmark),
            leaderboard=self.leaderboard(benchmark, group),
            judgments=self.records('judgments', benchmark), next_pair=self.comparison(benchmark))

    def aggregate(self, benchmarks, group='configuration'):
        if group not in ('configuration', 'model') or not benchmarks or len(set(benchmarks)) != len(benchmarks):
            raise ValueError('Choose distinct benchmarks and model/configuration grouping')
        boards = [self.leaderboard(k, group) for k in benchmarks]
        common = set.intersection(*(set(r['id'] for r in board) for board in boards))
        # Compare the same opponents on every selected task. Never average task
        # ratings fitted against unequal opponent pools. Missing evidence yields
        # no overall score. Every dated run must have at least one cross-group vote.
        rows = []
        for key in common:
            cells, covered, run_covered, run_total, count = [], 0, 0, 0, 0
            for benchmark, board in zip(benchmarks, boards):
                identities = {m['id']: r['id'] for r in board for m in r['members']}
                members = next(r['members'] for r in board if r['id'] == key)
                run_total += len(members)
                seen_runs = set()
                opponents = {k: [] for k in common if k != key}
                for v in self.records('judgments', benchmark):
                    a, b = identities.get(v['left']), identities.get(v['right'])
                    if a == b or a not in common or b not in common or key not in (a, b):
                        continue
                    y = .5 if v['preference'] == 'tie' else float((v['preference'] == 'left') == (a == key))
                    opponents[b if a == key else a].append(y)
                    seen_runs.add(v['left'] if a == key else v['right'])
                    count += 1
                run_covered += len(seen_runs)
                if opponents and all(opponents.values()):
                    covered += 1
                    cells.extend(sum(values)/len(values) for values in opponents.values())
            complete = covered == len(benchmarks) and run_covered == run_total and bool(cells)
            rows.append(dict(identity=key, score=sum(cells)/len(cells) if complete else None,
                             judgments=count, evaluated_benchmarks=covered,
                             judged_runs=run_covered, total_runs=run_total))
        return dict(benchmarks=benchmarks, coverage=len(benchmarks),
            policy='Balanced observed preference against the same common opponents, equal task/opponent weight, all dated runs. No overall score until every task/opponent and every run has evidence.',
            rows=sorted(rows, key=lambda r: (r['score'] is None, -(r['score'] or 0), r['identity'])))
