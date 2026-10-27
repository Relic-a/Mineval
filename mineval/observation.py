"""Read-only, loopback-only HTML over validated local records; no execution API."""
import difflib
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import tempfile
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from . import benchmark, evaluation
from .artifacts import digest, read_json, relative, unpack
from .preparation import identifier
from .review import checked

TEXT_LIMIT = 256 * 1024
CSS = """
:root {color-scheme:light; --paper:#f5f4ef; --ink:#242d2a; --muted:#53645e; --line:#c6cfc8; --accent:#235d48}
*{box-sizing:border-box} body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 system-ui,sans-serif}
header{border-bottom:1px solid var(--line);padding:18px max(24px,calc((100vw - 1200px)/2));display:flex;gap:32px;align-items:center;flex-wrap:wrap}
header strong{font-size:21px;letter-spacing:-.025em} header span{color:var(--muted);font-size:14px} nav{display:flex;gap:24px;flex-wrap:wrap}
main{max-width:1248px;margin:auto;padding:36px 24px 72px} h1{font-size:34px;line-height:1.2;letter-spacing:-.025em;overflow-wrap:anywhere;margin:12px 0 20px} h2{font-size:22px;margin:40px 0 12px} h3{font-size:17px;margin:24px 0 8px}
a{color:var(--accent);text-underline-offset:3px} a:hover{color:#123a2c} a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #956519;outline-offset:4px}
p{max-width:75ch} .muted,small{color:var(--muted)} .notice{padding:14px 18px;background:#e7ece6;border:1px solid var(--line)}
.scroll{overflow:auto} .task-pool table{min-width:1050px} .task-pool td:nth-child(3){min-width:250px} .task-pool td:nth-child(4){min-width:90px} table{border-collapse:collapse;width:100%;text-align:left;font-variant-numeric:tabular-nums} td,th{padding:14px 16px 14px 0;border-bottom:1px solid var(--line);vertical-align:top} th{font-size:13px;color:var(--muted);font-weight:600} td:first-child{min-width:140px} td{overflow-wrap:anywhere} code{font:13px/1.5 ui-monospace,monospace;overflow-wrap:anywhere}
pre{background:#e9ede7;border:1px solid var(--line);padding:18px;overflow:auto;max-height:650px;font:13px/1.6 ui-monospace,monospace;tab-size:4} details{margin:12px 0} summary{cursor:pointer;color:var(--accent);padding:8px 0} .status{white-space:nowrap;font-size:13px;font-weight:600;color:#235d48} .status.fail,.status.failed,.status.error,.status.rejected{color:#9c3026} .status.retired,.status.interrupted,.status.inconclusive,.status.needs-review{color:#775313}
.trail{font-size:14px;display:flex;gap:14px;flex-wrap:wrap;margin-bottom:24px} label{display:block;margin:12px 0} input,select,button,textarea{max-width:100%;font:inherit;accent-color:var(--accent)} button{background:var(--accent);color:white;padding:10px 20px;border:0;cursor:pointer} button:hover{background:#123a2c} fieldset{border:1px solid var(--line);padding:18px;min-width:0} .columns{display:grid;grid-template-columns:1fr 1fr;gap:24px} ul{padding-left:22px} li{margin:8px 0} ::selection{background:#c4d9cc;color:#192d23} :root{scrollbar-color:#8c9b90 var(--paper)}
@media(max-width:640px){header{gap:12px;padding:16px 20px} nav{width:100%} main{padding:24px 20px 48px} h1{font-size:28px}.columns{grid-template-columns:1fr} td,th{padding:12px 12px 12px 0}}
"""


def esc(value):
    return escape(str(value), quote=True)


def pretty(value):
    return '<pre>' + esc(json.dumps(value, indent=2, ensure_ascii=False)) + '</pre>'


def status(value):
    return f'<span class="status {esc(value)}">{esc(value)}</span>'


def table(headers, rows):
    rows = list(rows)
    if not rows:
        return '<p class="muted">No records yet.</p>'
    return '<div class="scroll"><table><thead><tr>' + ''.join('<th scope="col">' + esc(h) + '</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + c + '</td>' for c in row) + '</tr>' for row in rows) + '</tbody></table></div>'


class Pages:
    def __init__(self, store, prefix='', workflows=None, csrf=''):
        self.store, self.prefix = store, prefix
        self.workflows, self.csrf = workflows, csrf

    def url(self, *parts, **query):
        return self.prefix + '/' + '/'.join(quote(str(p), safe='') for p in parts) + ('?' + urlencode(query, doseq=True) if query else '')

    def link(self, text, *parts, **query):
        return '<a href="' + esc(self.url(*parts, **query)) + '">' + esc(text) + '</a>'

    def page(self, title, content):
        arena_link = ('<a href="' + esc(self.prefix[:-len('/history')] + '/') + '">Web Arena</a>'
                      if self.prefix.endswith('/history') else self.link('Web Arena', 'arena'))
        return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + esc(title) + ' · Mineval</title><style>' + CSS + '</style><body><header><strong>Mineval</strong><nav aria-label="Main">' + self.link('Tasks') + self.link('Models', 'models') + self.link('Queue', 'jobs') + self.link('Suites', 'suites') + self.link('Compare', 'compare') + arena_link + '</nav><span>Local observation · private human view</span></header><main><h1>' + esc(title) + '</h1>' + content + '</main></body></html>'

    def task_link(self, task):
        return self.link(task, 'tasks', task)

    def revision_link(self, ref):
        return self.link(ref['version'], 'tasks', ref['task_id'], 'revisions', ref['version'])

    def trail(self, task, ref=None, run=None):
        items = [self.link('Task pool'), self.task_link(task)]
        if ref:
            items.append(self.revision_link(ref))
        if run:
            items.append(self.link(run, 'tasks', task, 'runs', run))
        return '<div class="trail" aria-label="Record links">' + ' / '.join(items) + '</div>'

    def files(self, task, scope, record, root):
        paths = [p for p in sorted(root.rglob('*')) if p.is_file()]
        return '<ul>' + ''.join('<li>' + self.link(p.relative_to(root).as_posix(), 'tasks', task, 'artifacts', scope, record, path=p.relative_to(root).as_posix()) + '</li>' for p in paths) + '</ul>'

    def artifact_root(self, task, scope, record):
        """Only receipt-checked, explicit artifact namespaces can be browsed."""
        identifier(record)
        base = self.store.task(task)
        if scope == 'revisions':
            self.store._inspect_revision(task, record)
            return base / 'versions' / record
        if scope == 'runs':
            benchmark.inspect_attempt(self.store, task, record)
            root = benchmark.output_path(self.store, task, record)
        elif scope == 'evaluations':
            evaluation.inspect(self.store, task, record)
            root = base / 'evaluation-evidence' / record
        elif scope in ('cleanups', 'extractions', 'reviews', 'extraction-attempts'):
            root = base / ('cleanups' if scope == 'cleanups' else 'private/' + scope) / record
        else:
            raise ValueError('Unknown artifact namespace')
        checked(root)
        return root

    def render(self, path, query=None):
        query = query or {}
        parts = [p for p in path.split('/') if p]
        if not parts:
            from .dashboard import pool
            return pool(self, query)
        if parts == ['models']:
            from .dashboard import models
            return models(self, query)
        if parts == ['jobs']:
            from .dashboard import jobs
            return jobs(self)
        if parts == ['arena']:
            if self.prefix.endswith('/history'):
                return self.page('Web Arena', '<p><a href="' + esc(self.prefix[:-len('/history')] + '/') + '">Return to the Web Arena workspace</a></p>')
            return self.page('Web Arena', '<p>Persistent design briefs, interactive websites, human preference rankings, and share images.</p><p>Open the Arena workspace with <code>mineval --data ' + esc(self.store.root) + ' arena serve --image YOUR_PRELOADED_WEB_IMAGE</code>. It also includes this historical view.</p>')
        if parts == ['suites']:
            return self.page('Suites', '<p>Each suite pins exact prepared revisions. Pool changes leave these selections intact.</p>' + table(['Suite', 'Description', 'Tasks'], ((self.link(s['suite_id'], 'suites', s['suite_id']), esc(s['description']), str(len(s['members']))) for s in self.store.list_suites())))
        if len(parts) == 2 and parts[0] == 'suites':
            suite = self.store.inspect_suite(parts[1])
            return self.page('Suite ' + parts[1], table(['Task', 'Prepared revision'], ((self.task_link(r['task_id']), self.revision_link(r)) for r in suite['members'])) + pretty(suite))
        if parts == ['compare']:
            return self.comparison(query)
        if len(parts) < 2 or parts[0] != 'tasks':
            raise ValueError('Page does not exist')
        task = identifier(parts[1])
        if len(parts) == 2:
            return self.task_page(task)
        with self.store.operation(task, allow_rejected=True):
            if len(parts) == 4 and parts[2] == 'revisions':
                ref = self.store._inspect_revision(task, identifier(parts[3]))
                root = self.artifact_root(task, 'revisions', parts[3])
                body = self.trail(task) + '<p>Frozen preparation, review and private verification. These files are available only in this human observation view.</p>' + pretty(ref)
                for title, name in (('Cleanup findings', 'cleanup-report.json'),
                        ('Independent review', 'approval.json'),
                        ('Private verification provenance and quality', 'private/verification/extraction-manifest.json'),
                        ('Fresh-environment diagnostic', 'last-diagnostic.json')):
                    if (root / name).exists():
                        body += '<h2>' + title + '</h2>' + pretty(read_json(root / name))
                body += '<h2>All frozen artifacts</h2>'
                body += self.files(task, 'revisions', parts[3], root)
                return self.page('Prepared revision ' + parts[3], body)
            if len(parts) == 4 and parts[2] == 'runs':
                run = benchmark.inspect_attempt(self.store, task, identifier(parts[3]))
                body = self.trail(task, run['prepared_revision'])
                body += '<p>Harness termination: ' + status(run['status']) + '. Task success requires an independent evaluation.</p>'
                if run.get('suite'):
                    body += '<p>Suite: ' + self.link(run['suite']['suite_id'], 'suites', run['suite']['suite_id']) + '</p>'
                body += '<p>Model + harness: <strong>' + esc(run['model']['name'] + ' / ' + run['harness']['name']) + '</strong></p>'
                completion = run.get('agent_result', {}).get('completion') or {}
                if completion:
                    body += '<p>' + esc(completion.get('summary', '')) + '</p><p class="muted">Historical solution seen: ' + esc(completion.get('historical_solution_seen', 'unknown')) + ' (self-reported).</p>'
                assessments = [evaluation.inspect(self.store, task, p.stem) for p in sorted((self.store.task(task) / 'evaluations').glob('*.json'))]
                body += '<h2>Evaluations</h2>' + table(['Assessment', 'Verdict'], ((self.link(e['evaluation_id'], 'tasks', task, 'evaluations', e['evaluation_id']), status(e.get('verdict', e['status']))) for e in assessments if e['run_id'] == parts[3]))
                if run.get('output_available'):
                    body += '<h2>Output changes</h2>' + self.diff(task, run)
                    body += '<h2>Saved output artifacts</h2>' + self.files(task, 'runs', parts[3], self.artifact_root(task, 'runs', parts[3]))
                body += '<details><summary>Full run identity and termination record</summary>' + pretty(run) + '</details>'
                return self.page('Run ' + parts[3], body)
            if len(parts) == 4 and parts[2] == 'evaluations':
                record = evaluation.inspect(self.store, task, identifier(parts[3]))
                body = self.trail(task, record.get('prepared_revision'), record['run_id'])
                body += '<p>Verdict: ' + status(record.get('verdict', record['status'])) + '</p><p>' + esc(record.get('explanation', 'Identity reservation only.')) + '</p>'
                body += table(['Private check', 'Outcome', 'Exit code'], ((esc(c['name']), status(c['status']), esc(c.get('exit_code'))) for c in record.get('commands', [])))
                if record.get('limitations'):
                    body += '<h2>Evidence limitations</h2><ul>' + ''.join('<li>' + esc(x) + '</li>' for x in record['limitations']) + '</ul>'
                if record.get('adaptations'):
                    body += '<h2>Justified adaptations</h2>' + pretty(record['adaptations'])
                if record['schema_version'] == 2:
                    body += '<h2>Private evidence</h2><p>Original checks, execution results and justified adaptations are retained here.</p>' + self.files(task, 'evaluations', parts[3], self.artifact_root(task, 'evaluations', parts[3]))
                body += '<details><summary>Full assessment and provenance record</summary>' + pretty(record) + '</details>'
                return self.page('Evaluation ' + parts[3], body)
            if len(parts) == 5 and parts[2] == 'artifacts':
                root = self.artifact_root(task, parts[3], parts[4])
                name = query.get('path', [''])[0]
                target = root.joinpath(*relative(name).parts)
                # checked() above rejects symlinks anywhere in this namespace.
                if not target.is_file():
                    raise ValueError('Artifact does not exist')
                body = self.trail(task)
                if target.suffix in ('.gz', '.pack'):
                    body += '<p>Binary archive. Inspect its hash manifest here; export run code with <code>mineval bench export TASK RUN DESTINATION</code>.</p>'
                else:
                    with target.open('rb') as stream:
                        data = stream.read(TEXT_LIMIT + 1)
                    if len(data) > TEXT_LIMIT:
                        body += '<p class="notice">Preview truncated at 256 KiB. Read the full artifact in the local store.</p>'
                    body += '<pre>' + esc(data[:TEXT_LIMIT].decode('utf-8', errors='replace')) + '</pre>'
                return self.page(name, body)
        raise ValueError('Page does not exist')

    def task_page(self, task):
        record = self.store.inspect_task(task)
        body = self.trail(task) + '<p>' + esc(record['metadata']['task_summary']) + '</p><p>Status: ' + status(record['state']['status']) + '</p>'
        if self.workflows:
            from .dashboard import form, hidden
            body += '<p>' + self.link('Select models and run this task', search=task) + '</p>'
            body += form(self, 'selection', hidden('task', task) + '<button name="operation" value="retire">Remove from active pool…</button>')
        body += '<details><summary>Candidate provenance and state history</summary>' + pretty({'metadata': record['metadata'], 'state': record['state']}) + '</details>'
        body += '<h2>Prepared revisions</h2>' + table(['Revision', 'Inspection'], ((self.link(v, 'tasks', task, 'revisions', v), 'Cleanup, independent review and private verification') for v in record['prepared_revisions']))
        body += '<h2>Preparation history</h2>'
        with self.store.operation(task, allow_rejected=True):
            for scope in ('cleanups', 'extractions', 'extraction-attempts', 'reviews'):
                parent = self.store.task(task) / ('cleanups' if scope == 'cleanups' else 'private/' + scope)
                for directory in sorted(parent.glob('*')):
                    root = self.artifact_root(task, scope, directory.name)
                    name = {'cleanups': 'cleanup-report.json', 'extractions': 'extraction-manifest.json',
                            'extraction-attempts': 'report.json', 'reviews': 'review.json'}[scope]
                    body += '<details><summary>' + esc(scope + ' / ' + directory.name) + '</summary>' + pretty(read_json(root / name)) + self.files(task, scope, directory.name, root) + '</details>'
            body += '<h3>Fresh-environment diagnostics</h3>'
            for path in sorted((self.store.task(task) / 'diagnostics').glob('*.json')):
                if path.name.endswith('.sha256.json'):
                    continue
                if digest(path) != read_json(path.with_suffix('.sha256.json'))['sha256']:
                    raise ValueError('Diagnostic artifact hash mismatch')
                body += '<details><summary>' + esc(path.stem) + '</summary>' + pretty(read_json(path)) + '</details>'
        body += '<h2>Benchmark runs</h2>'
        runs = [self.store.inspect_run(task, Path(name).stem) for name in record['runs']]
        body += table(['Run', 'Model + harness', 'State', 'Revision'], ((self.link(r['run_id'], 'tasks', task, 'runs', r['run_id']), esc(r['model']['name'] + ' / ' + r['harness']['name']), status(r['status']), self.revision_link(r['prepared_revision'])) for r in runs))
        body += '<h2>Evaluations</h2>'
        results = [self.store.inspect_evaluation(task, Path(name).stem) for name in record['evaluations']]
        body += table(['Assessment', 'Run', 'Verdict'], ((self.link(e['evaluation_id'], 'tasks', task, 'evaluations', e['evaluation_id']), self.link(e['run_id'], 'tasks', task, 'runs', e['run_id']), status(e.get('verdict', e['status']))) for e in results))
        body += '<p class="muted">To exclude this task from future work: <code>mineval task retire ' + esc(task) + ' --reason "Reason"</code>. Old results remain available.</p>'
        return self.page(task, body)

    def diff(self, task, run, *, text=False):
        self.store._inspect_revision(task, run['prepared_revision']['version'])
        with tempfile.TemporaryDirectory(prefix='mineval-observe-') as temp:
            temp = Path(temp)
            frozen = self.store.task(task) / 'versions' / run['prepared_revision']['version']
            unpack(frozen / 'snapshot.tar.gz', temp / 'before')
            unpack(benchmark.output_path(self.store, task, run['run_id']) / 'agent/workspace.tar.gz', temp / 'after')
            names = sorted({p.relative_to(temp / side).as_posix() for side in ('before', 'after') for p in (temp / side).rglob('*') if p.is_file()})
            output, budget = [], TEXT_LIMIT
            for name in names:
                paths = [temp / side / name for side in ('before', 'after')]
                if any(p.is_file() and p.stat().st_size > TEXT_LIMIT for p in paths):
                    if (all(p.is_file() for p in paths) and digest(paths[0]) == digest(paths[1])
                            and (paths[0].stat().st_mode & 0o111) == (paths[1].stat().st_mode & 0o111)):
                        continue
                    diff = name + ': large file changed; inspect exported output\n'
                else:
                    contents = [p.read_bytes() if p.is_file() else b'' for p in paths]
                    if contents[0] == contents[1] and paths[0].is_file() == paths[1].is_file() and (not paths[0].is_file() or (paths[0].stat().st_mode & 0o111) == (paths[1].stat().st_mode & 0o111)):
                        continue
                    if any(b'\0' in c for c in contents):
                        diff = name + ': binary content changed\n'
                    else:
                        diff = ''.join(difflib.unified_diff(*(c.decode('utf-8', errors='replace').splitlines(keepends=True) for c in contents), fromfile='prepared/' + name, tofile='candidate/' + name))
                        if not diff:
                            diff = name + ': file presence or executable mode changed\n'
                encoded = diff.encode('utf-8')
                output.append(encoded[:budget].decode('utf-8', errors='replace')); budget -= len(encoded)
                if budget <= 0:
                    output.append('\nPreview truncated; export output for the full changes.'); break
            content = ''.join(output) or 'No file changes.'
            return content if text else '<pre>' + esc(content) + '</pre>'

    def comparison(self, query):
        kind = query.get('kind', ['evaluations'])[0]
        if kind not in ('runs', 'evaluations'):
            raise ValueError('Choose runs or evaluations')
        refs = []
        for task in self.store.list_tasks():
            task_id = task['task_id']
            listing = self.store.inspect_task(task_id)
            for name in listing[kind]:
                record_id = Path(name).stem
                r = (self.store.inspect_run(task_id, record_id) if kind == 'runs' else self.store.inspect_evaluation(task_id, record_id))
                if r['schema_version'] == 2 and r['status'] not in ('reserved', 'running'):
                    run = r if kind == 'runs' else self.store.inspect_run(task_id, r['run_id'])
                    model = run['model']
                    harness = run['harness']
                    refs.append((task_id + ':' + record_id, task_id + ' / ' + record_id + ' · ' + model.get('name', '') + ' / ' + harness.get('name', '')))
        body = '<p>Select one record per task on each side and one model + harness per side. Rates use only exact common revisions with matching evaluation policies.</p><p>' + self.link('Compare evaluations', 'compare', kind='evaluations') + ' · ' + self.link('Compare runs', 'compare', kind='runs') + '</p>'
        body += '<form method="get" action="' + esc(self.url('compare')) + '"><input type="hidden" name="kind" value="' + kind + '"><div class="columns">'
        for side in ('left', 'right'):
            body += '<fieldset><legend>' + side.title() + ' selection</legend>'
            for value, label in refs:
                body += '<label><input type="checkbox" name="' + side + '" value="' + esc(value) + '"' + (' checked' if value in query.get(side, []) else '') + '> ' + esc(label) + '</label>'
            if not refs:
                body += '<p>No finished records available.</p>'
            body += '</fieldset>'
        body += '</div><p><button type="submit">Compare selections</button></p></form>'
        if query.get('left') or query.get('right'):
            definition = {}
            for side in ('left', 'right'):
                definition[side] = []
                for value in query.get(side, []):
                    task, record = value.split(':', 1)
                    definition[side].append({'task_id': task, 'run_id' if kind == 'runs' else 'evaluation_id': record})
            result = (benchmark.compare if kind == 'runs' else evaluation.compare)(self.store, definition)
            coverage = result['coverage']
            body += '<h2>Common-task coverage</h2><p class="notice">' + str(coverage['common']) + ' exact common revisions · ' + str(coverage['left_total']) + ' selected left · ' + str(coverage['right_total']) + ' selected right. Different subsets do not establish a comparable overall ranking.</p>'
            body += table(['Common task', 'Prepared revision'], ((self.task_link(r['task_id']), self.revision_link(r)) for r in result['common_revisions']))
            if 'rates' in result:
                def rate(value):
                    return 'Unavailable (no common tasks)' if value is None else f'{value:.1%}'
                body += '<h2>Verdicts on the common set</h2>' + table(['Side', 'Pass', 'Fail', 'Inconclusive', 'Error', 'Pass rate', 'Decided coverage'], ((side.title(), *(str(r['counts'][k]) for k in ('pass', 'fail', 'inconclusive', 'error')), esc(rate(r['pass_rate'])), esc(rate(r['decided_coverage']))) for side, r in result['rates'].items()))
            body += '<details><summary>Exact selections, policies and coverage record</summary>' + pretty(result) + '</details>'
        return self.page('Compare ' + kind, body)


def make_server(store, port=0, workflows=None):
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError('Port must be between 0 and 65535')
    prefix = '/' + secrets.token_urlsafe(24)
    pages = Pages(store, prefix, workflows, secrets.token_urlsafe(32))
    import threading
    worker_guard = threading.Lock()
    worker_thread = None
    worker_stop = threading.Event()
    def start_worker():
        nonlocal worker_thread
        if workflows is None:
            return
        with worker_guard:
            if worker_thread is None or not worker_thread.is_alive():
                def work():
                    while not worker_stop.is_set():
                        try:
                            workflows.work(stop_event=worker_stop)
                        except KeyboardInterrupt:
                            break
                        except ValueError:
                            pass  # Another local CLI/server worker owns the queue.
                        worker_stop.wait(0.5)
                worker_thread = threading.Thread(target=work, daemon=False)
                worker_thread.start()
    pages.start_worker = start_worker
    # Do not resume interrupted inference implicitly; work() marks uncertain jobs.
    start_worker()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Keep private URLs, source names and evidence out of access logs.

        def do_POST(self):
            if workflows is None:
                self.send_error(501); return
            parsed = urlsplit(self.path)
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if (self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}'
                    or self.headers.get('Origin') != origin
                    or self.headers.get('Sec-Fetch-Site', 'same-origin') != 'same-origin'
                    or not parsed.path.startswith(prefix + '/actions/')
                    or self.headers.get('Content-Type') != 'application/x-www-form-urlencoded'):
                self.send_error(403); return
            submitted_id = None
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 128 * 1024:
                    raise ValueError('Invalid request size')
                fields = parse_qs(self.rfile.read(size).decode('utf-8'), max_num_fields=4000)
                if not secrets.compare_digest(fields.get('csrf', [''])[0], pages.csrf):
                    self.send_error(403); return
                from .dashboard import action
                if workflows.submit_once(fields.get('request_id', [''])[0]):
                    submitted_id = fields['request_id'][0]
                    body = action(pages, parsed.path[len(prefix + '/actions/'):], fields)
                else:
                    body = pages.page('Request already received', '<p>This form was already submitted. ' + pages.link('View queue', 'jobs') + '</p>')
                code = 200
            except (ValueError, OSError, KeyError, TypeError) as exc:
                if submitted_id is not None:
                    workflows.release_submission(submitted_id)
                code = 400
                body = pages.page('Unable to submit work', '<p class="notice">' + esc(exc) + '</p>' + pages.link('Return to tasks'))
            self.respond(code, body)

        def respond(self, code, body):
            data = body.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'same-origin' if workflows else 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urlsplit(self.path)
            if (self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')
                    or not (parsed.path == prefix or parsed.path.startswith(prefix + '/'))):
                self.send_error(404); return
            try:
                from urllib.parse import unquote
                body = pages.render(unquote(parsed.path[len(prefix):]), parse_qs(parsed.query))
                code = 200
            except (ValueError, OSError, KeyError, TypeError) as exc:
                code = 400
                body = pages.page('Unable to inspect record', '<p class="notice">' + esc(exc) + '</p><p>Check the selection, artifact receipts, or whether a CLI operation currently holds the task lock. Refresh to retry.</p>' + pages.link('Return to tasks'))
            data = body.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'same-origin' if workflows else 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    original_close = server.server_close
    def close():
        worker_stop.set()
        original_close()
        if worker_thread is not None:
            # Finish the bounded current sandbox stage so its context cleans up.
            # The stage checkpoint stops subsequent inference and leaves work retryable.
            worker_thread.join()
    server.server_close = close
    server.observation_url = f'http://127.0.0.1:{server.server_port}{prefix}/'
    return server


def serve(store, port=0, workflows=None):
    with make_server(store, port, workflows) as server:
        print('Human observation (contains private verification): ' + server.observation_url, flush=True)
        print('Stop with Ctrl-C. Historical workflow progress is durable.', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
