"""Exercise the real subprocess protocol with a local fixture, never a model."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mineval.agents import CodexAdapter
from mineval.codex_runner import run


class CodexRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / 'inputs'; self.inputs.mkdir()
        self.control = self.root / 'control'; self.control.mkdir()
        CodexAdapter(effort='high').stage(self.inputs, required_files=('cleanup-report.json', 'verify.json'))
        self.prompt = 'Original task with literal shell text: $(touch unwanted) `false`'
        (self.inputs / 'prompt.txt').write_text(self.prompt)
        executable = self.root / 'codex'
        executable.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
calls = root / 'calls.jsonl'
prior = calls.read_text().splitlines() if calls.exists() else []
with calls.open('a') as stream:
    stream.write(json.dumps({'argv': sys.argv[1:], 'prompt': sys.stdin.read()}) + '\\n')
code = int(os.environ.get('RESUME_EXIT' if prior else 'FIXTURE_EXIT', '0'))
if code:
    raise SystemExit(code)
if not os.environ.get('NO_THREAD'):
    print(json.dumps({'type': 'thread.started', 'thread_id': 'exact-thread-id'}))
print('non-json fixture noise')
print('[]')
mode = os.environ.get('FIXTURE_MODE', 'repair')
names = ['final.json', 'cleanup-report.json', 'verify.json']
if mode == 'complete' or (mode == 'repair' and prior):
    for name in names:
        (root / 'control' / name).write_text('{}')
''')
        executable.chmod(0o755)
        self.env = patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH'],
                                           'FIXTURE_ROOT': str(self.root)})
        self.env.start(); self.addCleanup(self.env.stop)

    def execute(self):
        return run(['--model', 'fixture-model', '-c', 'model_reasoning_effort="high"'],
                   self.inputs, self.control)

    def calls(self):
        return [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()]

    def test_missing_files_resume_exact_thread_once_with_original_options(self):
        self.assertEqual(self.execute(), 0)
        first, second = self.calls()
        self.assertEqual(first['prompt'], self.prompt)
        self.assertNotIn('resume', first['argv'])
        self.assertEqual(second['argv'][-3:], ['resume', 'exact-thread-id', '-'])
        for call in (first, second):
            for flag in ('--ignore-user-config', '--ignore-rules', '--yolo', '--json', '--output-schema'):
                self.assertIn(flag, call['argv'])
            self.assertNotIn('--ephemeral', call['argv'])
            self.assertIn('fixture-model', call['argv'])
            self.assertIn('model_reasoning_effort="high"', call['argv'])
        for name in ('final.json', 'cleanup-report.json', 'verify.json'):
            self.assertIn(str(self.control / name), second['prompt'])
        self.assertNotIn('--last', second['argv'])
        self.assertFalse((self.root / 'unwanted').exists())

    def test_complete_first_turn_needs_no_reminder(self):
        with patch.dict(os.environ, {'FIXTURE_MODE': 'complete'}):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(len(self.calls()), 1)

    def test_still_missing_after_reminder_fails_without_a_third_turn(self):
        with patch.dict(os.environ, {'FIXTURE_MODE': 'omit'}):
            self.assertEqual(self.execute(), 1)
        self.assertEqual(len(self.calls()), 2)

    def test_nonzero_exit_is_not_retried(self):
        with patch.dict(os.environ, {'FIXTURE_EXIT': '7'}):
            self.assertEqual(self.execute(), 7)
        self.assertEqual(len(self.calls()), 1)

    def test_no_thread_fails_without_starting_a_fresh_session(self):
        with patch.dict(os.environ, {'NO_THREAD': '1'}):
            self.assertEqual(self.execute(), 1)
        self.assertEqual(len(self.calls()), 1)

    def test_failed_resume_is_not_retried(self):
        with patch.dict(os.environ, {'RESUME_EXIT': '9'}):
            self.assertEqual(self.execute(), 9)
        self.assertEqual(len(self.calls()), 2)

    def test_existing_malformed_files_are_left_to_stage_validation(self):
        for name in ('final.json', 'cleanup-report.json', 'verify.json'):
            (self.control / name).write_text('invalid JSON')
        with patch.dict(os.environ, {'FIXTURE_MODE': 'omit'}):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(len(self.calls()), 1)

    def test_existing_reports_are_not_requested_again(self):
        (self.control / 'final.json').write_text('{}')
        (self.control / 'verify.json').write_text('{}')
        self.assertEqual(self.execute(), 0)
        self.assertIn('cleanup-report.json', self.calls()[1]['prompt'])
        self.assertNotIn('verify.json', self.calls()[1]['prompt'])
        self.assertNotIn('final.json', self.calls()[1]['prompt'])

    def test_stage_rejects_required_files_outside_control(self):
        with self.assertRaises(ValueError):
            CodexAdapter().stage(self.inputs, required_files=('../escape.json',))
