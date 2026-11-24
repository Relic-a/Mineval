"""Publish functional assessments independently of optional review work.

Superseded or interrupted internal evaluation IDs remain excluded.
"""
import sqlite3
import json
from contextlib import closing


def published(store, task, run_id, evaluation_id=None):
    path = store.root / 'historical-jobs.sqlite3'
    if not path.exists():
        return True
    with closing(sqlite3.connect(path, timeout=15)) as db:
        rows = db.execute("SELECT state,json_extract(payload,'$.internal_evaluation_ids') FROM jobs WHERE kind='benchmark' AND task=? AND json_extract(payload,'$.run_id')=?", (task, run_id)).fetchall()
    if rows and evaluation_id is not None:
        from mineval.core.artifacts import read_json
        from mineval.core.store import identifier
        record = store.task(task) / 'evaluations' / (identifier(evaluation_id) + '.json')
        if not record.exists() or not record.with_suffix('.sha256').exists():
            return False
        if read_json(record).get('error_kind') == 'interrupted':
            return False
    return all(evaluation_id not in json.loads(internal or '[]') for state, internal in rows)
