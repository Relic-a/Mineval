"""Read-only Codex app-server catalog, following Aproekt's discovery handshake."""
import json
import os
from pathlib import Path
import queue
from mineval.core import processes as subprocess
import tempfile
import threading
import time

from mineval.core.agents import provision_auth


class Catalog:
    def __init__(self, auth_file=None):
        self.auth_file = auth_file
        self._lock = threading.Lock()
        self._cached = None
        self._at = 0

    def get(self, refresh=False):
        with self._lock:
            ttl = 15 if self._cached and self._cached.get('error') else 300
            if not refresh and self._cached is not None and time.monotonic() - self._at < ttl:
                return self._cached
            try:
                result = {'models': discover(self.auth_file), 'error': None}
            except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
                # Provider diagnostics can contain credentials or private config paths.
                result = {'models': [], 'error': 'Codex model discovery failed. Check that the host Codex CLI is installed and signed in, then refresh. Explicit model identifiers remain available.'}
            self._cached, self._at = result, time.monotonic()
            return result


def discover(auth_file=None, *, command=('codex', 'app-server', '--stdio'), timeout=30):
    """No turns or inference. Isolate config and copy only model credentials."""
    with tempfile.TemporaryDirectory(prefix='mineval-models-') as directory:
        home = Path(directory)
        source = Path(auth_file) if auth_file else Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'auth.json'
        if source.exists():
            provision_auth(source, home / 'auth.json')
        elif auth_file:
            raise ValueError('Configured credential file does not exist')
        env = dict(os.environ, CODEX_HOME=directory)
        process = subprocess.Popen(command, cwd=directory, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        messages = queue.Queue(maxsize=1024)
        done = threading.Event()

        def read():
            try:
                for line in process.stdout:
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    while not done.is_set():
                        try:
                            messages.put(value, timeout=.1)
                            break
                        except queue.Full:
                            pass
            finally:
                if not done.is_set():
                    try:
                        messages.put_nowait(None)
                    except queue.Full:
                        pass

        def drain():
            while process.stderr.read(8192):
                pass

        readers = [threading.Thread(target=read, daemon=True), threading.Thread(target=drain, daemon=True)]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + timeout
        sequence = 0

        def send(value):
            process.stdin.write(json.dumps(value) + '\n')
            process.stdin.flush()

        def request(method, params):
            nonlocal sequence
            sequence += 1
            send({'id': sequence, 'method': method, 'params': params})
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Codex catalog timed out')
                try:
                    message = messages.get(timeout=min(remaining, .2))
                except queue.Empty:
                    if process.poll() is not None:
                        raise ValueError('Codex app-server exited')
                    continue
                if message is None:
                    raise ValueError('Codex app-server exited')
                if not isinstance(message, dict):
                    continue
                if 'method' in message:
                    if 'id' in message:
                        send({'id': message['id'], 'error': {'code': -32601, 'message': 'Discovery only'}})
                    continue
                if message.get('id') == sequence:
                    if 'error' in message or not isinstance(message.get('result'), dict):
                        raise ValueError('Invalid Codex response')
                    return message['result']

        try:
            request('initialize', {'clientInfo': {'name': 'mineval', 'title': 'Mineval', 'version': '0.1.0'},
                                   'capabilities': {'experimentalApi': True}})
            send({'method': 'initialized'})
            models, seen, cursors = [], set(), set()
            params = {}
            while True:
                response = request('model/list', params)
                if not isinstance(response.get('data'), list):
                    raise ValueError('Invalid model catalog')
                for item in response['data']:
                    if not isinstance(item, dict) or item.get('hidden'):
                        continue
                    identifier = item.get('model') or item.get('id')
                    if not isinstance(identifier, str) or not identifier or identifier in seen:
                        continue
                    seen.add(identifier)
                    efforts = [e['reasoningEffort'] for e in item.get('supportedReasoningEfforts', [])
                               if isinstance(e, dict) and isinstance(e.get('reasoningEffort'), str)]
                    models.append({'model': identifier, 'name': item.get('displayName') or identifier,
                                   'efforts': efforts, 'default_effort': item.get('defaultReasoningEffort') or 'medium',
                                   'is_default': bool(item.get('isDefault'))})
                cursor = response.get('nextCursor')
                if not cursor:
                    return models
                if not isinstance(cursor, str) or cursor in cursors or len(cursors) >= 100:
                    raise ValueError('Invalid catalog pagination')
                cursors.add(cursor)
                params = {'cursor': cursor}
        finally:
            done.set()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            for reader in readers:
                reader.join(timeout=2)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
