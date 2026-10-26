"""Arena harness/build/preview lifecycle. No generated command runs on the host."""
import base64
from contextlib import ExitStack, contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import shutil
import threading
import time
from urllib.parse import urlsplit

from .agents import CodexAdapter, provision_auth
from .arena import now
from .artifacts import inventory, unpack, write_json
from .sandbox import DockerSandbox, Limits, SandboxSpec

STATIC_SERVER = '''import http.server, os
class Handler(http.server.SimpleHTTPRequestHandler):
 def do_GET(self):
  if not os.path.isfile(self.translate_path(self.path)) and not '.' in self.path.rsplit('/',1)[-1]: self.path='/index.html'
  super().do_GET()
os.chdir('dist' if os.path.isdir('dist') else '.')
http.server.ThreadingHTTPServer(('0.0.0.0',3000), Handler).serve_forever()
'''
FIXTURE = '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><style>
body{margin:0;background:#f3efe4;color:#283f31;font:20px/1.5 Georgia;padding:8vw}h1{font-size:clamp(40px,8vw,100px);line-height:1}button,input{font:inherit;padding:12px}button{background:#283f31;color:white;border:0;cursor:pointer}section{min-height:65vh}a{color:inherit}</style>
<section><p>Interactive fixture · no model call</p><h1>A place to<br>make something.</h1><p id="message">A real website, ready to explore.</p><input aria-label="Your name" placeholder="Your name"><button onclick="document.querySelector('#message').textContent='Welcome, '+document.querySelector('input').value">Say hello</button><p><a href="/explore">Explore the next page</a></p></section><section><h2>Keep exploring.</h2><p>Scroll, type, click. Every generation is preserved.</p></section>'''


def normalize_tree(root):
    """Quiesced agent output only. Materialize internal dependency symlinks;
    reject external links, cycles and special files before receipt/archiving.
    """
    root = Path(root).resolve()
    git = root / '.git'
    if git.is_symlink() or git.is_file():
        git.unlink()
    elif git.exists():
        shutil.rmtree(git)
    links = [p for p in root.rglob('*') if p.is_symlink()]
    for p in links:
        if not p.is_symlink():
            continue
        target = p.resolve(strict=True)
        if root not in target.parents or target == p or target in p.parents:
            raise ValueError('Output contains an external or cyclic link')
        # Dependencies can contain links; reject directory link recursion rather
        # than following arbitrary trees while privileged to the store.
        if target.is_dir():
            if any(x.is_symlink() for x in target.rglob('*')):
                raise ValueError('Nested directory links are unsupported')
            p.unlink(); shutil.copytree(target, p)
        elif target.is_file():
            p.unlink()
            if p.parent.name == '.bin':
                import shlex
                p.write_text('#!/bin/sh\nexec ' + shlex.quote('/workspace/' + target.relative_to(root).as_posix()) + ' "$@"\n')
                p.chmod(0o755)
            else:
                shutil.copyfile(target, p); p.chmod(target.stat().st_mode & 0o777)
        else:
            raise ValueError('Output contains a special file')
    inventory(root)


def runtime_config(root):
    path = root / 'arena-runtime.json'
    cfg = json.loads(path.read_text()) if path.exists() else {'build': [], 'start': ['python3', '-c', STATIC_SERVER]}
    if not isinstance(cfg, dict) or set(cfg) - {'build', 'start'}:
        raise ValueError('arena-runtime.json supports build and start only')
    builds, start = cfg.get('build', []), cfg.get('start')
    if not isinstance(builds, list) or len(builds) > 8:
        raise ValueError('At most eight build commands')
    for argv in [*builds, start]:
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or '\0' in x for x in argv):
            raise ValueError('Runtime commands must be nonempty argv arrays')
    return {'build': builds, 'start': start}


class Runner:
    def __init__(self, arena, image, context=None, auth_file=None, timeout=600, sandbox=None):
        self.arena, self.image, self.context = arena, image, context
        self.auth_file, self.timeout = auth_file, timeout
        self.sandbox = sandbox or DockerSandbox(context=context)
        self.cancelled = threading.Event()
        self.active = None
        self.active_lock = threading.Lock()

    @contextmanager
    def session(self, spec):
        with self.sandbox.container(spec) as c:
            with self.active_lock:
                if self.cancelled.is_set():
                    raise ValueError('Generation interrupted during server shutdown')
                self.active = c
                try:
                    c.start()
                except BaseException:
                    self.active = None
                    raise
            try:
                yield c
            finally:
                with self.active_lock:
                    self.active = None

    def cancel(self):
        self.cancelled.set()
        with self.active_lock:
            if self.active and self.active.inspect()['Running']:
                self.active._cli.call('kill', self.active.id)

    def generate(self, key):
        import tempfile
        limits = Limits(memory_mb=2048, tmp_mb=256, timeout_seconds=self.timeout)
        r = self.arena.transition(key, 'queued', status='running', started_at=now())
        try:
            task = self.arena.get('benchmarks', r['benchmark'])
            source = self.arena.check_inputs(task)
            with tempfile.TemporaryDirectory(prefix='mineval-web-') as tmp:
                tmp = Path(tmp)
                work, inputs, control = tmp / 'workspace', tmp / 'inputs', tmp / 'control'
                if (source / 'starter').exists():
                    shutil.copytree(source / 'starter', work)
                else:
                    work.mkdir()
                if (source / 'assets').exists():
                    shutil.copytree(source / 'assets', work / 'arena-assets')
                inputs.mkdir(); control.mkdir()
                prompt = f'''Build a complete website for the following design task in /workspace.
{task['prompt']}
References: {task['references']}
Supplied assets are in arena-assets/ when present. Preserve them.
Run/build the real application. Create arena-runtime.json containing a JSON object:
"build": a list of argv arrays to install locked dependencies and build the application;
"start": an argv array that serves the production application on 0.0.0.0 port 3000.
Python3 is available for serving static sites. For npm projects commit a package-lock.json,
use npm ci and npm run build; start a production server, not a development/HMR server.
The final website will run offline in a fresh isolated copy with its built dependencies.
Do not depend on remote fonts, CDN scripts, live APIs or services. Vendor needed assets.
Do not write secrets into the website. No historical Git task or private tests exist.
At completion return the required completion JSON with a concise summary.
'''
                (inputs / 'prompt.txt').write_text(prompt)
                cfg = r['config']
                if cfg['harness'] == 'fixture':
                    command = ('python3', '-c', 'from pathlib import Path; Path("index.html").write_text(' + repr(FIXTURE.replace('#283f31', '#294d70' if cfg['effort'] == 'high' else '#283f31')) + ')')
                else:
                    if self.auth_file:
                        provision_auth(self.auth_file, inputs / 'auth.json')
                    command = CodexAdapter(cfg['model']).stage(inputs)
                    # Reuse invocation/credential isolation, not the historical
                    # benchmark's completion schema or solution-lookup policy.
                    write_json(inputs / 'schema.json', {'type': 'object', 'properties': {
                        'summary': {'type': 'string'}}, 'required': ['summary'], 'additionalProperties': False})
                    command = (*command[:2], command[2].replace('--model "$1"', '--model "$1" -c "$2"'),
                               *command[3:], 'model_reasoning_effort=' + json.dumps(cfg['effort']))
                spec = SandboxSpec(self.image, command, limits, workspace=work, inputs=inputs,
                                   control=control, network='bridge', retain_logs=False)
                with self.session(spec) as c:
                    timed_out = c.wait(self.timeout)
                    state = c.inspect()
                    image_id = c.image_id
                    if timed_out or state.get('OOMKilled') or state['ExitCode'] != 0:
                        raise ValueError('Agent timed out, exhausted memory, or failed')
                if cfg['harness'] == 'codex':
                    completion = json.loads((control / 'final.json').read_text())
                    if not isinstance(completion, dict) or set(completion) != {'summary'} or not isinstance(completion['summary'], str) or len(completion['summary']) > 16000:
                        raise ValueError('Invalid Arena harness completion report')
                else:
                    completion = {'summary': 'Deterministic interactive fixture; no real model call'}
                normalize_tree(work)
                runtime = runtime_config(work)
                # Dependency installation/build gets no credentials or benchmark store.
                build_results = []
                if runtime['build']:
                    build_spec = SandboxSpec(image_id, ('sleep', 'infinity'), limits,
                                             workspace=work, network='bridge')
                    with self.session(build_spec) as c:
                        for argv in runtime['build']:
                            result = c.exec(argv, timeout=self.timeout)
                            build_results.append(result)
                            if result['exit_code'] != 0 or result['timed_out'] or result['oom_killed']:
                                raise ValueError('Website build failed: ' + result['stderr'][-2000:])
                    normalize_tree(work)
                # Smoke the actual production server offline before declaring ready.
                smoke_spec = SandboxSpec(image_id, tuple(runtime['start']), limits,
                                         workspace=work, network='none')
                with self.session(smoke_spec) as c:
                    for _ in range(60):
                        probe = c.exec(('python3', '-c', FETCH, json.dumps(dict(path='/', method='GET', body='', headers={}))), timeout=20)
                        if probe['exit_code'] == 0:
                            response = json.loads(probe['stdout'])
                            if response['status'] < 500:
                                break
                        if not c.inspect()['Running']:
                            raise ValueError('Website production server exited during startup')
                        time.sleep(.25)
                    else:
                        raise ValueError('Website production server did not become ready offline')
                normalize_tree(work)
                return self.arena.publish(key, work, image_id=image_id, runtime=runtime,
                    build_results=build_results, completion=completion, instructions_sha256=__import__('hashlib').sha256(prompt.encode()).hexdigest())
        except KeyboardInterrupt:
            self.arena.transition(key, 'running', status='error', finished_at=now(), error='Generation cancelled by operator; create an explicit rerun.')
            raise
        except Exception as exc:
            return self.arena.transition(key, 'running', status='error', finished_at=now(), error=str(exc)[:4000])


# HTTP is relayed through docker exec; preview containers have no network interface
# beyond loopback and publish no Docker ports. Production HTTP applications work
# without HMR/WebSockets. Each app receives its own browser origin and secret URL.
FETCH = '''import urllib.request, urllib.error, json, base64, sys
cfg=json.loads(sys.argv[1])
req=urllib.request.Request('http://127.0.0.1:3000'+cfg['path'],data=base64.b64decode(cfg['body']) if cfg['body'] else None,method=cfg['method'],headers=cfg['headers'])
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args): return None
try: r=urllib.request.build_opener(NoRedirect).open(req,timeout=15)
except urllib.error.HTTPError as e: r=e
body=r.read(8*1024*1024+1)
if len(body)>8*1024*1024: raise ValueError('Preview response exceeds 8 MiB')
print(json.dumps({'status':r.code,'headers':list(r.headers.items()),'body':base64.b64encode(body).decode()}))
'''


class Preview:
    def __init__(self, arena, key, sandbox):
        import tempfile
        self.stack = ExitStack()
        try:
            self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='mineval-preview-')))
            unpack(arena.artifact(key), self.temp / 'workspace')
            r = arena.get('generations', key)
            spec = SandboxSpec(r['image_id'], tuple(r['runtime']['start']), Limits(memory_mb=2048, tmp_mb=128),
                               workspace=self.temp / 'workspace', network='none')
            self.container = self.stack.enter_context(sandbox.container(spec))
            self.container.start()
            for _ in range(60):
                try:
                    self.fetch('/', 'GET', b'', {})
                    break
                except Exception:
                    if not self.container.inspect()['Running']:
                        raise ValueError('Website server exited; check its runtime start command')
                    time.sleep(.25)
            else:
                raise ValueError('Website server did not become ready')
            token = secrets.token_urlsafe(24)
            preview = self
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_GET(self): self.relay()
                def do_POST(self): self.relay()
                def do_PUT(self): self.relay()
                def do_PATCH(self): self.relay()
                def do_DELETE(self): self.relay()
                def do_HEAD(self): self.relay()
                def relay(self):
                    if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                        self.send_error(404); return
                    # First visit establishes a preview-only capability cookie. It
                    # is never shared with the private management server (no cookies there).
                    if urlsplit(self.path).path == '/' + token:
                        self.send_response(303)
                        self.send_header('Set-Cookie', f'arena_preview_{self.server.server_port}={token}; HttpOnly; SameSite=Strict; Path=/')
                        self.send_header('Location', '/')
                        self.send_header('Referrer-Policy', 'no-referrer')
                        self.end_headers(); return
                    if f'arena_preview_{self.server.server_port}={token}' not in self.headers.get('Cookie', '').split('; '):
                        self.send_error(404); return
                    try:
                        length = int(self.headers.get('Content-Length', 0))
                        if not 0 <= length <= 2*1024*1024: raise ValueError('Request too large')
                        headers = {k: v for k, v in self.headers.items() if k.lower() in ('content-type', 'accept')}
                        cookie_prefix = f'arena_app_{self.server.server_port}_'
                        cookies = [part[len(cookie_prefix):] for part in self.headers.get('Cookie', '').split('; ') if part.startswith(cookie_prefix)]
                        if cookies: headers['Cookie'] = '; '.join(cookies)
                        result = preview.fetch(self.path, self.command, self.rfile.read(length), headers)
                        body = base64.b64decode(result['body'])
                        self.send_response(result['status'])
                        for k, v in result['headers']:
                            if k.lower() in ('content-type', 'location', 'set-cookie'):
                                if k.lower() == 'location' and (v.startswith('//') or urlsplit(v).scheme):
                                    continue
                                if k.lower() == 'set-cookie':
                                    # Cookies are host-scoped, unlike browser origins.
                                    # Namespace application cookies per preview port;
                                    # never expose another app's capability or state.
                                    parts = v.split(';')
                                    parts[0] = f'arena_app_{self.server.server_port}_' + parts[0].strip()
                                    parts = [part for part in parts if not part.strip().lower().startswith(('domain=', 'path='))]
                                    v = ';'.join(parts) + '; Path=/; SameSite=Strict'
                                self.send_header(k, v)
                        self.send_header('Content-Length', str(len(body)))
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('Referrer-Policy', 'no-referrer')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        self.send_header('Content-Security-Policy', "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'self'; form-action 'self'; worker-src 'none'")
                        self.end_headers()
                        if self.command != 'HEAD': self.wfile.write(body)
                    except Exception:
                        self.send_error(502, 'Preview unavailable; reopen the saved website to retry')
            self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.url = f'http://127.0.0.1:{self.server.server_port}/{token}'
        except BaseException:
            self.stack.close()
            raise

    def fetch(self, path, method, body, headers):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('Invalid preview path')
        cfg = json.dumps(dict(path=path, method=method, body=base64.b64encode(body).decode(), headers=headers))
        r = self.container.exec(('python3', '-c', FETCH, cfg), timeout=20)
        if r['exit_code'] != 0:
            raise ValueError('Preview HTTP request failed')
        return json.loads(r['stdout'])

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(5)
        self.stack.close()


class RuntimeManager:
    def __init__(self, arena, sandbox):
        self.arena, self.sandbox, self.previews = arena, sandbox, {}
        self.lock = threading.Lock()

    def open(self, key):
        with self.lock:
            if key in self.previews:
                try:
                    if not self.previews[key].container.inspect()['Running']:
                        raise ValueError('Runtime exited')
                    self.previews[key].fetch('/', 'GET', b'', {})
                except Exception:
                    self.previews.pop(key).close()
            if key not in self.previews:
                if len(self.previews) >= 6:
                    oldest = next(iter(self.previews))
                    self.previews.pop(oldest).close()
                self.previews[key] = Preview(self.arena, key, self.sandbox)
            return self.previews[key].url

    def close(self):
        with self.lock:
            for p in self.previews.values(): p.close()
            self.previews.clear()
