import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mineval.core.artifacts import digest, write_json
from mineval.historical.evaluation import execute_checks
from mineval.historical.publication import published


class FixedGradingReviewTests(unittest.TestCase):
    def test_adaptations_are_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, 'cannot be adapted'):
                execute_checks(root, root / 'workspace', root / 'evidence', root,
                               [], [{'path': 'test', 'content': 'changed'}], {}, '', None)
            self.assertFalse((root / 'evidence').exists())

    def test_review_state_does_not_hide_completed_assessment(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SimpleNamespace(root=Path(temp), task=lambda task: Path(temp) / 'tasks' / task)
            evaluations = store.task('task') / 'evaluations'
            evaluations.mkdir(parents=True)
            for evaluation_id, result in (
                ('current', {'verdict': 'pass'}),
                ('old', {'verdict': 'pass'}),
                ('interrupted', {'verdict': 'error', 'error_kind': 'interrupted'}),
            ):
                record = evaluations / (evaluation_id + '.json')
                write_json(record, result)
                write_json(record.with_suffix('.sha256'), {'sha256': digest(record)})
            self.assertTrue(published(store, 'task', 'run', 'old'))
            with sqlite3.connect(store.root / 'historical-jobs.sqlite3') as db:
                db.execute('CREATE TABLE jobs (kind TEXT, task TEXT, state TEXT, payload TEXT)')
                db.execute('INSERT INTO jobs VALUES (?, ?, ?, ?)',
                           ('benchmark', 'task', 'failed', json.dumps({'run_id': 'run', 'internal_evaluation_ids': ['old']})))
            self.assertTrue(published(store, 'task', 'run', 'current'))
            self.assertFalse(published(store, 'task', 'run', 'old'))
            self.assertFalse(published(store, 'task', 'run', 'interrupted'))


if __name__ == '__main__':
    unittest.main()
