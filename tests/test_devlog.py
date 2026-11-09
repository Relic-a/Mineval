"""Diagnostics must reveal failures without leaking raw text into normal logs."""
import json
import io
from contextlib import redirect_stderr
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from mineval import devlog, processes


class DevlogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.handlers = {}
        self.stream_patch = patch.object(devlog, '_streams', self.handlers)
        self.stream_patch.start()
        self.addCleanup(self.stream_patch.stop)
        self.directory_patch = patch.object(devlog, '_directory', None)
        self.directory_patch.start()
        self.addCleanup(self.directory_patch.stop)
        self.addCleanup(lambda: [h.close() for h in self.handlers.values()])
        self.env = patch.dict(os.environ, {'MINEVAL_LOG_DIR': self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.root = devlog.configure()

    def records(self, stream):
        return [json.loads(x) for x in (self.root / (stream + '.jsonl')).read_text().splitlines()]

    def test_nonzero_process_separates_raw_output_and_correlates_events(self):
        result = processes.run([sys.executable, '-c',
                               'import sys; print("synthetic-secret"); sys.exit(7)'],
                              capture_output=True)
        self.assertEqual(result.returncode, 7)
        public = (self.root / 'dev.jsonl').read_text() + (self.root / 'prod.jsonl').read_text()
        self.assertNotIn('synthetic-secret', public)
        self.assertIn('synthetic-secret', (self.root / 'sensitive.jsonl').read_text())
        start, end = self.records('dev')
        self.assertEqual(start['process_id'], end['process_id'])
        self.assertEqual(end['exit_code'], 7)
        self.assertGreaterEqual(end['duration_seconds'], 0)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        for path in self.root.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_popen_preserves_output_and_records_exit_once(self):
        with processes.Popen([sys.executable, '-c', 'print("private-popen")'],
                             stdout=processes.PIPE, text=True) as proc:
            self.assertEqual(proc.communicate()[0], 'private-popen\n')
        self.assertEqual(sum(r['event'] == 'process.end' for r in self.records('dev')), 1)
        self.assertIn('private-popen', (self.root / 'sensitive.jsonl').read_text())
        self.assertNotIn('private-popen', (self.root / 'dev.jsonl').read_text())

    def test_timeout_keeps_exception_and_private_traceback(self):
        with self.assertRaises(processes.TimeoutExpired):
            processes.run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.01)
        self.assertEqual(self.records('prod')[-1]['error_type'], 'TimeoutExpired')
        self.assertIn('Traceback', (self.root / 'sensitive.jsonl').read_text())

    def test_rotation_and_unavailable_logging_do_not_change_result(self):
        for handler in self.handlers.values():
            handler.maxBytes = 250
        for _ in range(10):
            devlog.event('test', sensitive='private' * 20)
        self.assertTrue((self.root / 'sensitive.jsonl.1').exists())
        self.assertFalse((self.root / 'sensitive.jsonl.3').exists())
        with patch.object(devlog, 'configure', side_effect=OSError('disk full')):
            self.assertEqual(processes.run([sys.executable, '-c', 'pass']).returncode, 0)

    def test_handler_failure_does_not_dump_sensitive_record_to_stderr(self):
        stream = io.StringIO()
        with patch.object(self.handlers['sensitive'].stream, 'write', side_effect=OSError('disk full')):
            with redirect_stderr(stream):
                devlog.event('test', sensitive='must-stay-private')
        self.assertEqual(stream.getvalue(), '')
