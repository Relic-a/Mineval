import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mineval.cli import _main


class HistoricalImagePrecedenceTests(unittest.TestCase):
    def test_observe_only_constructs_a_queue_in_operations_mode(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch('mineval.core.configuration.load', return_value={}), \
             patch('mineval.workflows.Workflows') as workflow, \
             patch('mineval.server.serve_historical') as serve:
            self.assertEqual(_main(['--data', tmp, 'observe']), 0)
            workflow.assert_not_called()
            self.assertNotIn('workflows', serve.call_args.kwargs)

    def test_workflow_and_observe_image_precedence(self):
        for action in ('workflow', 'observe'):
            for source, settings_image, cli_image, expected in (
                ('explicit settings', 'settings:image', None, 'settings:image'),
                ('explicit override', 'settings:image', 'cli:image', 'cli:image'),
                ('saved fallback', None, None, 'saved:image'),
                ('implicit settings', 'settings:image', None, 'settings:image'),
            ):
                with self.subTest(action=action, source=source), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    data = root / 'data'
                    data.mkdir()
                    settings = (data / 'historical-settings.json' if source == 'implicit settings'
                                else root / 'settings.json')
                    settings.write_text(json.dumps({'preparation_image': settings_image} if settings_image else {}))
                    argv = ['--data', str(data), action]
                    if source != 'implicit settings':
                        argv += ['--settings', str(settings)]
                    if cli_image:
                        argv += ['--image', cli_image]
                    if action == 'workflow':
                        argv += ['work']
                    else:
                        argv += ['--operations']
                    with patch('mineval.core.configuration.load', return_value={'image': 'saved:image'}), \
                         patch('mineval.workflows.Workflows') as workflow, \
                         patch('mineval.server.serve_historical') as serve, \
                         contextlib.redirect_stdout(io.StringIO()):
                        workflow.return_value.jobs.return_value = []
                        self.assertEqual(_main(argv), 0)
                    self.assertEqual(workflow.call_args.args[1]['preparation_image'], expected)
                    if action == 'workflow':
                        workflow.return_value.work.assert_called_once()
                        serve.assert_not_called()
                    else:
                        self.assertIs(serve.call_args.kwargs['workflows'], workflow.return_value)


if __name__ == '__main__':
    unittest.main()
