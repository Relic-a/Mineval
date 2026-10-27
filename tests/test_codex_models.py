"""Catalog protocol fixtures launch a fake executable; no inference or network."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from mineval.codex_models import Catalog, discover
from mineval.dashboard import action
from mineval.observation import Pages
from mineval.preparation import Store
from mineval.workflows import Workflows


class DiscoveryTests(unittest.TestCase):
    def test_handshake_pagination_efforts_and_stderr_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / 'server.py'
            fake.write_text('''import json, sys
sys.stderr.write('x' * 512 * 1024); sys.stderr.flush()
for line in sys.stdin:
    m = json.loads(line)
    if m['method'] == 'initialize':
        assert m['params']['clientInfo']['name'] == 'mineval'
        result = {}
    elif m['method'] == 'initialized':
        continue
    elif m['method'] == 'model/list':
        if not m['params'].get('cursor'):
            result = {'data': [{'id':'internal', 'model':'model-a', 'displayName':'Model A', 'supportedReasoningEfforts':[{'reasoningEffort':'ultra'}], 'defaultReasoningEffort':'ultra'}, {'model':'hidden', 'hidden':True}], 'nextCursor':'next'}
        else:
            result = {'data':[{'model':'model-a'}, {'model':'model-b'}], 'nextCursor':None}
    print(json.dumps({'method':'notification'}), flush=True)
    print(json.dumps({'id':m['id'], 'result':result}), flush=True)
''')
            with patch.dict('os.environ', {'CODEX_HOME': directory}):
                result = discover(command=(sys.executable, str(fake)), timeout=5)
            self.assertEqual([m['model'] for m in result], ['model-a', 'model-b'])
            self.assertEqual(result[0]['efforts'], ['ultra'])
            self.assertEqual(result[0]['default_effort'], 'ultra')

    def test_timeout_and_exit_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'CODEX_HOME': ''}):
            before = time.monotonic()
            with self.assertRaises(TimeoutError):
                discover(command=(sys.executable, '-c', 'import time; time.sleep(20)'), timeout=.1)
            self.assertLess(time.monotonic() - before, 3)
            with self.assertRaises(ValueError):
                discover(command=(sys.executable, '-c', 'pass'), timeout=2)

    def test_cache_refresh_and_safe_failure(self):
        catalog = Catalog()
        with patch('mineval.codex_models.discover', return_value=[{'model': 'a'}]) as probe:
            catalog.get(); catalog.get()
            self.assertEqual(probe.call_count, 1)
            catalog.get(refresh=True)
            self.assertEqual(probe.call_count, 2)
        with patch('mineval.codex_models.discover', side_effect=OSError('secret')):
            self.assertNotIn('secret', catalog.get(refresh=True)['error'])


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'store')
        self.flow = Workflows(self.store, {'preparation_image': 'trusted:image'})
        self.flow.catalog.get = lambda **kw: {'models': [{'model': 'model-a', 'name': 'Model A', 'efforts': ['high', 'ultra'], 'default_effort': 'high'}], 'error': None}

    def test_save_reload_and_queued_identity(self):
        pages = Pages(self.store, workflows=self.flow)
        body = action(pages, 'model-settings', {
            'preparation_model':['model-a'], 'preparation_effort':['ultra'],
            'evaluation_model':['model-b'], 'evaluation_effort':['high'],
            'preparation_image':['untrusted:image']})
        self.assertIn('Model settings saved', body)
        choices = self.flow.model_settings()
        self.assertEqual(choices['preparation']['model'], 'model-a')
        self.assertEqual(Workflows(self.store).model_settings(), choices)
        self.assertEqual(self.flow.settings['preparation_image'], 'trusted:image')
        payload = {'model_settings': choices}
        self.flow.save_model_settings({r: c | {'model':'changed'} for r,c in choices.items()})
        self.assertEqual(self.flow.role_adapter('preparation', payload).model, 'model-a')
        self.assertEqual(self.flow.role_adapter('evaluation', payload).effort, 'high')
        with self.assertRaises(ValueError):
            self.flow.save_model_settings({r:c | {'model':'--bad'} for r,c in choices.items()})
        self.assertEqual(self.flow.model_settings()['preparation']['model'], 'changed')

    def test_discovered_models_in_settings_and_separate_task_workspace(self):
        pages = Pages(self.store, workflows=self.flow)
        body = pages.render('/tasks')
        self.assertNotIn('name="configuration"', body)
        self.assertIn('model-a', pages.render('/settings'))
        self.assertIn('Prep / extract', pages.render('/settings'))
        self.flow.catalog.get = lambda **kw: {'models': [], 'error': 'Discovery unavailable'}
        self.assertIn('Discovery unavailable', pages.render('/settings'))
        self.assertIn('name="evaluation_model"', pages.render('/settings'))
