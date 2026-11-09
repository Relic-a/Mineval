"""Exercise the real subprocess protocol with a local fixture, never a model."""
import json
import os
import subprocess
import sys
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
if sys.argv[1:] == ['--version']:
    print('codex fixture')
    raise SystemExit(0)
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
calls = root / 'calls.jsonl'
prior = calls.read_text().splitlines() if calls.exists() else []
with calls.open('a') as stream:
    stream.write(json.dumps({'argv': sys.argv[1:], 'prompt': sys.stdin.read()}) + '\\n')
print('fixture stderr diagnostic', file=sys.stderr)
if os.environ.get('BROKEN_HOST'):
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'error',
        'message': 'Code Mode is unavailable because failed to spawn code-mode host'}}))
    raise SystemExit(0)
code = int(os.environ.get('RESUME_EXIT' if prior else 'FIXTURE_EXIT', '0'))
if code:
    raise SystemExit(code)
if not os.environ.get('NO_THREAD'):
    print(json.dumps({'type': 'thread.started', 'thread_id': 'exact-thread-id'}))
print('non-json fixture noise')
print('[]')
mode = os.environ.get('FIXTURE_MODE', 'repair')
names = ['final.json', 'cleanup-report.json', 'verify.json']
if mode in ('selection-repair', 'selection-omit'):
    (root / 'control' / 'final.json').write_text('{}')
    selection = {'strategy': 'focused', 'artifacts': ['test']}
    if mode == 'selection-repair' and prior:
        selection['evaluation_config'] = {'image': 'fixture', 'commands': [{'kind': 'tests', 'argv': ['true']}]}
    (root / 'control' / 'selection.json').write_text(json.dumps(selection))
if mode == 'complete' or (mode == 'repair' and prior):
    for name in names:
        (root / 'control' / name).write_text('{}')
''')
        executable.chmod(0o755)
        host = self.root / 'codex-code-mode-host'
        host.write_text('#!/bin/sh\nexit 0\n')
        host.chmod(0o755)
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

    def test_incomplete_extraction_is_repaired_in_same_session(self):
        CodexAdapter().stage(self.inputs, required_files=('selection.json',),
                             require_extraction_config=True)
        with patch.dict(os.environ, {'FIXTURE_MODE': 'selection-repair'}):
            self.assertEqual(self.execute(), 0)
        first, second = self.calls()
        self.assertEqual(second['argv'][-3:], ['resume', 'exact-thread-id', '-'])
        self.assertIn('evaluation_config', second['prompt'])
        self.assertIn('evaluation_config', json.loads((self.control / 'selection.json').read_text()))

    def test_incomplete_extraction_after_reminder_fails(self):
        CodexAdapter().stage(self.inputs, required_files=('selection.json',),
                             require_extraction_config=True)
        with patch.dict(os.environ, {'FIXTURE_MODE': 'selection-omit'}):
            self.assertEqual(self.execute(), 1)
        self.assertEqual(len(self.calls()), 2)

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

    def test_missing_host_fails_before_model_invocation(self):
        (self.root / 'codex-code-mode-host').unlink()
        # Do not find a developer's host binary elsewhere on PATH.
        with patch('mineval.codex_runner.shutil.which', side_effect=lambda name:
                   str(self.root / 'codex') if name == 'codex' else None):
            self.assertEqual(self.execute(), 125)
        self.assertFalse((self.root / 'calls.jsonl').exists())

    def test_unloadable_host_fails_before_model_invocation(self):
        host = self.root / 'codex-code-mode-host'
        host.write_text('#!/missing-runtime-loader\n')
        self.assertEqual(self.execute(), 125)
        self.assertFalse((self.root / 'calls.jsonl').exists())

    def test_host_nonzero_exit_fails_before_model_invocation(self):
        (self.root / 'codex-code-mode-host').write_text('#!/bin/sh\nexit 7\n')
        self.assertEqual(self.execute(), 125)
        self.assertFalse((self.root / 'calls.jsonl').exists())

    def test_runtime_probe_needs_no_auth_prompt_or_reports(self):
        result = subprocess.run([sys.executable, str(self.inputs / 'codex_runner.py'),
                                 '--check-runtime'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'calls.jsonl').exists())
        self.assertEqual(list(self.control.iterdir()), [])

    def test_code_mode_error_with_successful_process_exit_is_not_retried(self):
        with patch.dict(os.environ, {'BROKEN_HOST': '1'}):
            result = self.captured_execution()
        self.assertEqual(result.returncode, 125)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn('runtime_unavailable', result.stderr)
        self.assertNotIn('agent.reminder_sent', result.stderr)

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

    def captured_execution(self):
        return subprocess.run([sys.executable, '-c',
            'from pathlib import Path; import sys; from mineval.codex_runner import run; '
            'sys.exit(run([], Path(sys.argv[1]), Path(sys.argv[2])))',
            str(self.inputs), str(self.control)], capture_output=True, text=True)

    def test_diagnostics_show_reminder_both_exits_and_raw_events(self):
        result = self.captured_execution()
        self.assertEqual(result.returncode, 0)
        self.assertIn('fixture stderr diagnostic', result.stderr)
        self.assertIn('thread.started', result.stdout)
        events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith('{')]
        self.assertEqual(sum(e['event'] == 'agent.reminder_sent' for e in events), 1)
        self.assertEqual(sum(e['event'] == 'agent.exit' for e in events), 2)
        self.assertEqual(events[-1]['missing'], [])
        self.assertTrue(events[-1]['reminder_sent'])

    def test_diagnostics_preserve_initial_failure_reason_without_reminder(self):
        with patch.dict(os.environ, {'FIXTURE_EXIT': '7'}):
            result = self.captured_execution()
        self.assertEqual(result.returncode, 7)
        self.assertIn('fixture stderr diagnostic', result.stderr)
        self.assertIn('initial_process_failed', result.stderr)
        self.assertNotIn('agent.reminder_sent', result.stderr)

    def test_stage_rejects_required_files_outside_control(self):
        with self.assertRaises(ValueError):
            CodexAdapter().stage(self.inputs, required_files=('../escape.json',))
