"""Workflow evidence is internal until its required evaluation and review finish.

Legacy records and explicit CLI assessments have no workflow publication barrier.
The trusted queue is durable; a failed/interrupted review keeps its evidence private
from aggregate results until an explicit retry completes the remaining stages.
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
    return all(state == 'completed' and evaluation_id not in json.loads(internal or '[]') for state, internal in rows)
