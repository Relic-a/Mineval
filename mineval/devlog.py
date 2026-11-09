"""Local JSONL diagnostics. Only explicit metadata belongs in public streams."""
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import threading
import traceback
from uuid import uuid4

_lock = threading.RLock()
_streams = {}
_directory = None
_session = uuid4().hex


class PrivateHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, 'a', encoding='utf-8')

    def handleError(self, record):
        # logging's default handler prints the raw record to stderr on I/O failure.
        pass


def configure(directory=None):
    """One private directory per host process; never mount this into a sandbox."""
    global _directory
    with _lock:
        if _streams:
            return _directory
        root = Path(directory or os.environ.get('MINEVAL_LOG_DIR') or
                    Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'mineval/logs')
        target = root / _session
        target.mkdir(parents=True, mode=0o700, exist_ok=True)
        target.chmod(0o700)
        handlers = {}
        try:
            for name in ('prod', 'dev', 'sensitive'):
                path = target / (name + '.jsonl')
                handler = PrivateHandler(path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding='utf-8')
                handler.setFormatter(logging.Formatter('%(message)s'))
                handlers[name] = handler
        except OSError:
            for handler in handlers.values():
                handler.close()
            raise
        _streams.update(handlers)
        _directory = target
        return target


def event(name, *, level='info', sensitive=None, **fields):
    """Callers supply safe metadata; arbitrary text goes only in sensitive."""
    try:
        with _lock:
            configure()
            base = dict(time=datetime.now(timezone.utc).isoformat(), session=_session,
                        pid=os.getpid(), thread=threading.get_ident(), event=name, level=level, **fields)
            def emit(stream, value):
                record = logging.LogRecord('mineval', logging.INFO, '', 0,
                                           json.dumps(value, default=str, ensure_ascii=True), (), None)
                _streams[stream].handle(record)
            emit('dev', base)
            if level in ('warning', 'error'):
                emit('prod', base)
            if sensitive is not None:
                emit('sensitive', base | {'details': sensitive})
    except (OSError, ValueError):
        # Disk exhaustion or diagnostics permissions must not change a verdict.
        pass


def failure(name, exc, **fields):
    event(name, level='error', error_type=type(exc).__name__,
          sensitive=''.join(traceback.format_exception(exc)), **fields)
