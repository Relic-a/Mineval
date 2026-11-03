"""Container-only Codex invocation with one missing-artifact follow-up.

Staged as a standalone script: no Mineval installation is needed in the image.
Session history lives only in the container's temporary CODEX_HOME. JSON events
are drained, never saved; only the exact thread ID is kept in memory.
"""
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


def invoke(arguments, prompt):
    thread_id = None
    # A file avoids stdin/stdout pipe deadlocks with large task prompts and
    # preserves the real exit code if Codex exits before reading stdin.
    with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as source:
        source.write(prompt)
        source.seek(0)
        with subprocess.Popen(['codex', 'exec', *arguments, '-'], stdin=source,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as process:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(event, dict) and event.get('type') == 'thread.started'
                        and isinstance(event.get('thread_id'), str)):
                    thread_id = event['thread_id']
            return process.wait(), thread_id


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


def run(options, inputs=Path('/mineval-input'), control=Path('/mineval-control')):
    common = ['--ignore-user-config', '--ignore-rules', '--yolo', '--json',
              '--output-schema', str(inputs / 'schema.json'),
              '--output-last-message', str(control / 'final.json'), *options]
    code, thread_id = invoke([*common, '--cd', '/workspace'], (inputs / 'prompt.txt').read_text())
    # A failed process is not a completed turn; never retry infrastructure errors.
    if code:
        return code
    required = [str(control / name) for name in json.loads((inputs / 'required-files.json').read_text())]
    missing = missing_files(required)
    if missing and thread_id:
        prompt = ('Your turn ended, but these required output files are missing:\n'
                  + '\n'.join(missing)
                  + '\nContinue the original task and write all required files using its '
                    'specified formats. Complete any required work and verification first; '
                    'report limitations truthfully. This is the only reminder. '
                    'Return the required completion JSON when finished.')
        code, _ = invoke([*common, 'resume', thread_id], prompt)
    # Missing files after the single reminder (or no resumable thread) still fail.
    return code or (1 if missing_files(required) else 0)


if __name__ == '__main__':
    raise SystemExit(run(sys.argv[1:]))
