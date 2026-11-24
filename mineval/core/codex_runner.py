"""Container-only Codex invocation with one missing-artifact follow-up.

Staged as a standalone script: no Mineval installation is needed in the image.
Raw events and stderr stream to the host's restricted sensitive diagnostics.
Session state stays in the container's temporary CODEX_HOME.
"""
import json
import os
import shutil
from datetime import datetime, timezone
import time
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


def progress(event, **fields):
    print(json.dumps(dict(time=datetime.now(timezone.utc).isoformat(),
                          event='agent.' + event, **fields)), file=sys.stderr, flush=True)


def check_runtime():
    """Offline probe shared by doctor and every staged Codex invocation."""
    try:
        codex = shutil.which('codex')
        if not codex:
            raise ValueError('codex is missing from PATH')
        sibling = Path(codex).resolve().with_name('codex-code-mode-host')
        host = str(sibling) if sibling.is_file() else shutil.which('codex-code-mode-host')
        if not host:
            raise ValueError('codex-code-mode-host is missing; install it beside Codex or on PATH')
        for executable, flag in ((codex, '--version'), (host, '--help')):
            result = subprocess.run([executable, flag], capture_output=True, text=True, timeout=10)
            if result.returncode:
                raise ValueError(f'{Path(executable).name} cannot run (exit {result.returncode}); '
                                 'check executable permissions and runtime dependencies')
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        progress('runtime_unavailable', reason=str(exc))
        return 125
    return 0


def invoke(arguments, prompt):
    thread_id = None
    runtime_failed = False
    started = time.monotonic()
    progress('start', resumed='resume' in arguments)
    # A file avoids stdin/stdout pipe deadlocks with large task prompts and
    # preserves the real exit code if Codex exits before reading stdin.
    with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as source:
        source.write(prompt)
        source.seek(0)
        with subprocess.Popen(['codex', 'exec', *arguments, '-'], stdin=source,
                              stdout=subprocess.PIPE, stderr=None, text=True) as process:
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(event, dict) and event.get('type') == 'thread.started'
                        and isinstance(event.get('thread_id'), str)):
                    thread_id = event['thread_id']
                if isinstance(event, dict):
                    item = event.get('item')
                    message = (item.get('message', '') if isinstance(item, dict)
                               and item.get('type') == 'error' else '')
                    if isinstance(message, str) and 'failed to spawn code-mode host' in message:
                        runtime_failed = True
            code = process.wait()
            if runtime_failed:
                progress('runtime_unavailable', reason='Codex could not spawn its code-mode host')
                code = code or 125
            progress('exit', exit_code=code, duration_seconds=time.monotonic() - started,
                     resumable=thread_id is not None)
            return code, thread_id


def missing_files(paths):
    missing = []
    for path in paths:
        try:
            present = stat.S_ISREG(Path(path).lstat().st_mode)
        except FileNotFoundError:
            present = False
        if not present:
            missing.append(path)
    return missing


def extraction_issues(inputs, control):
    """Check the stage-specific contract before ending the agent session."""
    contract = inputs / 'extraction-contract.json'
    if not contract.exists() or not json.loads(contract.read_text()).get('require_evaluation_config'):
        return []
    path = control / 'selection.json'
    if missing_files([path]):
        return []  # The ordinary missing-file reminder covers this case.
    try:
        plan = json.loads(path.read_text())
    except (ValueError, OSError):
        return [f'{path}: must be valid JSON']
    if not isinstance(plan, dict):
        return [f'{path}: must be a JSON object']
    if plan.get('strategy') != 'unavailable':
        config = plan.get('evaluation_config')
        if isinstance(config, dict) and config.get('grading_type') == 'native-script-v1':
            script = config.get('script')
            tests = config.get('required_tests')
            runnable = isinstance(script, str) and script.endswith('.sh') and isinstance(tests, list) and bool(tests)
        else:
            commands = config.get('commands') if isinstance(config, dict) else None
            runnable = isinstance(commands, list) and any(
                isinstance(c, dict) and c.get('kind') == 'tests'
                and isinstance(c.get('argv'), list) and c['argv'] for c in commands)
        if (not runnable or not isinstance(config.get('image'), str) or not config['image'].strip()):
            return [f'{path}: focused or fuller selection must include an evaluation_config object '
                    'with an image and runnable private tests, or use unavailable and explain why']
    return []


def run(options, inputs=Path('/mineval-input'), control=Path('/mineval-control')):
    code = check_runtime()
    if code:
        return code
    common = ['--ignore-user-config', '--ignore-rules', '--yolo', '--json',
              '--output-schema', str(inputs / 'schema.json'),
              '--output-last-message', str(control / 'final.json'), *options]
    code, thread_id = invoke([*common, '--cd', os.environ.get('MINEVAL_PROJECT_WORKDIR', '/workspace')], (inputs / 'prompt.txt').read_text())
    # A failed process is not a completed turn; never retry infrastructure errors.
    if code:
        progress('failed', reason='initial_process_failed', exit_code=code)
        return code
    required = [str(control / name) for name in json.loads((inputs / 'required-files.json').read_text())]
    missing = missing_files(required) + extraction_issues(inputs, control)
    progress('required_files', missing=missing)
    if missing and thread_id:
        progress('reminder_sent', missing=missing)
        prompt = ('Your turn ended, but these required outputs are missing or incomplete:\n'
                  + '\n'.join(missing)
                  + '\nContinue the original task and write all required files using its '
                    'specified formats. Complete any required work and verification first; '
                    'report limitations truthfully. This is the only reminder. '
                    'Return the required completion JSON when finished.')
        code, _ = invoke([*common, 'resume', thread_id], prompt)
    # Missing files after the single reminder (or no resumable thread) still fail.
    remaining = missing_files(required) + extraction_issues(inputs, control)
    result = code or (1 if remaining else 0)
    progress('finished', exit_code=result, missing=remaining,
             reminder_sent=bool(missing and thread_id))
    return result


if __name__ == '__main__':
    raise SystemExit(check_runtime() if sys.argv[1:] == ['--check-runtime'] else run(sys.argv[1:]))
