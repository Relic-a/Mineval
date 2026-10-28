import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from mineval import configuration, resources
from mineval.preparation import Store
from mineval.workflows import Workflows


class InstallationTests(unittest.TestCase):
    def test_settings_outside_repository(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'XDG_CONFIG_HOME': tmp}):
            path = configuration.config_path()
            path.parent.mkdir()
            path.write_text(json.dumps({'context': 'rootless', 'data': tmp}))
            self.assertEqual(configuration.load()['data'], tmp)

    def test_resource_admission(self):
        with patch('mineval.resources.capacity', return_value=(8, 8192)):
            self.assertEqual(resources.slots({}), 3)
            self.assertEqual(resources.slots({'resources': {'memory_mb_per_job': 8192}}), 0)
        with patch('mineval.resources.capacity', return_value=(2, 8192)):
            self.assertEqual(resources.slots({}), 1)

    def test_durable_independent_compaction_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'store')
            flow = Workflows(store)
            flow.schedule_compaction('model', {'image': 'local', 'auth_file': 'secret'})
            flow.schedule_compaction('model', {'image': 'local', 'auth_file': 'secret'})
            with flow.db() as db:
                rows = list(db.execute('SELECT * FROM maintenance'))
            self.assertEqual(len(rows), 1)
            self.assertNotIn('secret', rows[0]['options'])
            with patch('mineval.model_review.compact') as compact:
                Workflows(store).maintain()
            compact.assert_not_called()

    def test_failed_compaction_preserves_sources_and_retries_after_restart(self):
        from mineval.memory import Memory
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'store')
            memory = Memory(store)
            for i in range(24):
                memory.append('model', ['Observed behavior'], [{'evaluation_id': str(i)}], str(i))
            flow = Workflows(store)
            flow.schedule_compaction('model', {'image': 'local'})
            with patch('mineval.model_review.infer', side_effect=RuntimeError('interrupted')):
                with self.assertRaises(RuntimeError):
                    flow.maintain()
            self.assertEqual(len(memory.history('model')), 24)
            with patch('mineval.model_review.infer', return_value=['Merged observations']):
                Workflows(store).maintain()
            self.assertEqual(len(memory.history('model')), 23)
            self.assertEqual({source['evaluation_id'] for item in memory.history('model') for source in item['sources']}, {str(i) for i in range(24)})
