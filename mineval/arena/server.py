"""Private Arena UI/API. Separate from the read-only historical observation API."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit

from .store import Arena
from .runtime import Runner, RuntimeManager
from mineval.core.artifacts import digest, lock
from mineval.core.sandbox import SandboxError


class App:
    def __init__(self, arena, runner, manager):
        self.arena, self.runner, self.manager = arena, runner, manager
        self.stop = threading.Event()
        self.worker = None

    def work(self):
        # A single worker keeps batches predictable and avoids resource overcommit.
        while not self.stop.is_set():
            queued = next((r for r in self.arena.records('generations') if r['status'] == 'queued'), None)
            if queued:
                self.runner.generate(queued['id'])
            else:
                self.stop.wait(.5)

    def start(self):
        for r in self.arena.records('generations'):
            if r['status'] == 'running':
                self.arena.transition(r['id'], 'running', status='error', error='Server interrupted. This dated run is preserved; create an explicit rerun.')
        self.worker = threading.Thread(target=self.work, daemon=True)
        self.worker.start()

    def api(self, method, parts, data):
        a = self.arena
        if parts == ['benchmarks']:
            return a.records('benchmarks') if method == 'GET' else a.create(data)
        if parts == ['aggregate'] and method == 'POST':
            return a.aggregate(data.get('benchmarks', []), data.get('group', 'configuration'))
        if len(parts) == 2 and parts[0] == 'benchmarks' and method == 'GET':
            return a.summary(parts[1])
        if len(parts) == 3 and parts[0] == 'benchmarks' and method == 'POST':
            key, action = parts[1:]
            if action == 'generate': return a.enqueue(key, data.get('configs'))
            if action == 'judge': return a.judge(key, data.get('left'), data.get('right'), data.get('preference'), data.get('note', ''))
            if action == 'share':
                from .sharing import export
                return export(a, self.manager, key, data)
        if len(parts) == 2 and parts[0] == 'preview' and method == 'POST':
            return {'url': self.manager.open(parts[1])}
        raise ValueError('Unknown Arena endpoint')


def make_server(app, port=0, *, historical=None):
    prefix = '/' + secrets.token_urlsafe(24)
    static = Path(__file__).parents[1] / 'web'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self): self.handle_request('GET')
        def do_POST(self): self.handle_request('POST')
        def respond(self, code, body, mime='application/json', download=None, historical=False):
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            policy = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src http://127.0.0.1:*; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
            if historical:
                policy = policy.replace("style-src 'self'", "style-src 'self' 'unsafe-inline'")
            self.send_header('Content-Security-Policy', policy)
            if download: self.send_header('Content-Disposition', 'attachment; filename="' + download + '"')
            self.end_headers(); self.wfile.write(body)
        def handle_request(self, method):
            parsed = urlsplit(self.path)
            host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host or not parsed.path.startswith(prefix + '/'):
                self.send_error(404); return
            # Secret launch path plus exact Origin and custom header protects all
            # mutations from forms, cross-origin sites and generated previews.
            if method == 'POST' and (self.headers.get('Origin') != 'http://' + host or self.headers.get('X-Arena-Request') != '1' or self.headers.get('Content-Type') != 'application/json'):
                self.send_error(403); return
            route = parsed.path[len(prefix)+1:]
            try:
                if historical is not None and method == 'GET' and route.startswith('history/'):
                    body = historical(prefix + '/history', route[len('history'):], parsed.query)
                    self.respond(200, body, 'text/html; charset=utf-8', historical=True)
                    return
                if method == 'GET' and route in ('', 'arena.js', 'arena.css', 'historical.css'):
                    name = route or 'arena.html'
                    mime = {'arena.html': 'text/html; charset=utf-8', 'arena.js': 'text/javascript; charset=utf-8', 'arena.css': 'text/css; charset=utf-8', 'historical.css': 'text/css; charset=utf-8'}[name]
                    self.respond(200, (static / name).read_bytes(), mime); return
                if method == 'GET' and route.startswith('download/'):
                    key = route.split('/')[1]
                    r = app.arena.get('exports', key)
                    file = app.arena.root / 'shares' / (r['id'] + '.' + r['format'])
                    if digest(file) != r['sha256']: raise ValueError('Share hash mismatch')
                    self.respond(200, file.read_bytes(), 'image/' + r['format'], 'mineval-web-arena.' + r['format']); return
                data = {}
                if method == 'POST':
                    length = int(self.headers.get('Content-Length', 0))
                    if not 0 < length <= 72*1024*1024: raise ValueError('Request is empty or exceeds 72 MiB')
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict): raise ValueError('Expected a JSON object')
                if not route.startswith('api/'): raise ValueError('Page does not exist')
                result = app.api(method, route[4:].split('/'), data)
                self.respond(200, json.dumps(result).encode())
            except (BrokenPipeError, ConnectionResetError):
                return
            except (ValueError, OSError, KeyError, TypeError, SandboxError) as exc:
                self.respond(400, json.dumps({'error': str(exc)}).encode())
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.arena_url = f'http://127.0.0.1:{server.server_port}{prefix}/'
    return server


def serve(root, image, *, context=None, auth_file=None, port=0, timeout=600, historical=None):
    arena = Arena(root)
    with lock(arena.root / 'server.lock'):
        runner = Runner(arena, image, context, auth_file, timeout)
        manager = RuntimeManager(arena, runner.sandbox)
        app = App(arena, runner, manager)
        with make_server(app, port, historical=historical) as server:
            app.start()
            print('Mineval Web Arena (private): ' + server.arena_url, flush=True)
            print('Ctrl-C stops the server and previews. Queued runs resume next launch.', flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                app.stop.set()
                runner.cancel()
                # A worker owns its Docker context and must unwind before releasing
                # the single-server lock. Bound by the configured generation deadline.
                if app.worker: app.worker.join()
                manager.close()
