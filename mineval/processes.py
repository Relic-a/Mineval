"""Subprocess-compatible boundary with safe lifecycle and private diagnostics.

Do not capture new pipes or change process semantics for logging. Existing captured
output is recorded privately; live agent output is collected by the sandbox.
"""
import subprocess as _subprocess
import time
from uuid import uuid4
from .devlog import event, failure


def __getattr__(name):
    return getattr(_subprocess, name)


def run(*args, **kwargs):
    process_id = uuid4().hex
    started = time.monotonic()
    event('process.start', process_id=process_id, sensitive={'argv': args[0] if args else kwargs.get('args')})
    try:
        result = _subprocess.run(*args, **kwargs)
    except BaseException as exc:
        failure('process.error', exc, process_id=process_id, duration_seconds=time.monotonic() - started)
        raise
    event('process.end', process_id=process_id, exit_code=result.returncode,
          level='warning' if result.returncode else 'info', duration_seconds=time.monotonic() - started,
          sensitive={'stdout': _text(result.stdout), 'stderr': _text(result.stderr)})
    return result


def _text(value):
    if isinstance(value, bytes):
        value = value.decode(errors='replace')
    return value[:65536] if isinstance(value, str) else None


class Popen(_subprocess.Popen):
    def __init__(self, *args, **kwargs):
        self._log_id = uuid4().hex
        self._log_started = time.monotonic()
        self._log_done = False
        event('process.start', process_id=self._log_id,
              sensitive={'argv': args[0] if args else kwargs.get('args')})
        try:
            super().__init__(*args, **kwargs)
        except BaseException as exc:
            failure('process.error', exc, process_id=self._log_id)
            raise

    def poll(self):
        code = super().poll()
        if code is not None:
            self._ended(code)
        return code

    def wait(self, *args, **kwargs):
        code = super().wait(*args, **kwargs)
        self._ended(code)
        return code

    def communicate(self, *args, **kwargs):
        output = super().communicate(*args, **kwargs)
        event('process.output', process_id=self._log_id,
              sensitive={'stdout': _text(output[0]), 'stderr': _text(output[1])})
        return output

    def _ended(self, code):
        if not self._log_done:
            self._log_done = True
            event('process.end', process_id=self._log_id, exit_code=code,
                  level='warning' if code else 'info', duration_seconds=time.monotonic() - self._log_started)
