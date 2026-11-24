"""Arena persistence, incremental judgments, coverage, API boundaries and runtimes."""
import base64
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from mineval.arena import Arena
from mineval.arena.runtime import Runner, RuntimeManager, normalize_tree, runtime_config
from mineval.arena.server import App
from mineval.server import make_arena_server as make_server
from mineval.core.artifacts import digest, pack, unpack
from mineval.core.sandbox import DockerSandbox


class ArenaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.arena = Arena(self.root / 'store')
        self.task = self.arena.create({'title': 'Reading room', 'prompt': 'Build a reading app.',
            'assets': [{'name': 'reference.txt', 'data': base64.b64encode(b'reference').decode()}]})

    def enqueue(self, model='model-a', effort='medium', task=None):
        return self.arena.enqueue((task or self.task)['id'], [dict(model=model, effort=effort, harness='fixture')])[0]

    def ready(self, model='model-a', effort='medium', task=None):
        r = self.enqueue(model, effort, task)
        self.arena.transition(r['id'], 'queued', status='running')
        source = self.root / r['id']; source.mkdir()
        (source / 'index.html').write_text('<button>Interactive website</button>')
        return self.arena.publish(r['id'], source, image_id='sha256:fixture',
            runtime={'build': [], 'start': ['python3', '-m', 'http.server', '3000']})

    def test_inputs_generations_reruns_and_receipts_survive_reopen(self):
        first = self.ready(); second = self.ready()
        reopened = Arena(self.root / 'store')
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['config'], second['config'])
        self.assertNotEqual(first['created_at'], second['created_at'])
        self.assertEqual(reopened.get('generations', first['id']), first)
        with self.assertRaisesRegex(ValueError, 'state changed'):
            reopened.transition(first['id'], 'queued', status='running')
        archive = reopened.artifact(first['id'])
        archive.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            reopened.artifact(first['id'])
        source = reopened.check_inputs(self.task)
        (source / 'assets/reference.txt').write_text('tamper')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.enqueue()

    def test_batch_is_atomic_and_does_not_deduplicate_explicit_reruns(self):
        cfg = dict(model='a', effort='medium', harness='fixture')
        with self.assertRaises(ValueError): self.arena.enqueue(self.task['id'], [cfg, cfg | {'effort': 'invalid'}])
        self.assertEqual(self.arena.records('generations'), [])
        self.assertEqual(len(self.arena.enqueue(self.task['id'], [cfg, cfg])), 2)

    def test_judgments_ties_cycles_incremental_pairs_and_grouping(self):
        a, b, c = self.ready('a'), self.ready('b'), self.ready('c')
        task = self.task['id']
        self.arena.judge(task, a['id'], b['id'], 'left')
        self.arena.judge(task, b['id'], c['id'], 'left')
        self.arena.judge(task, c['id'], a['id'], 'left')
        board = self.arena.leaderboard(task)
        self.assertTrue(all(abs(r['score']) < .01 for r in board))
        self.assertEqual({r['position'] for r in board}, {1})
        self.assertIsNone(self.arena.comparison(task))
        with self.assertRaisesRegex(ValueError, 'already has'):
            self.arena.judge(task, b['id'], a['id'], 'tie')
        new = self.ready('a')
        pair = self.arena.comparison(task)
        self.assertIn(new['id'], pair.values())
        self.arena.judge(task, pair['left'], pair['right'], 'tie', 'Both work well')
        grouped = self.arena.leaderboard(task, 'configuration')
        members = next(r['members'] for r in grouped if r['config']['model'] == 'a')
        self.assertEqual(len(members), 2)
        self.assertEqual(len(Arena(self.root / 'store').records('judgments', task)), 4)
        self.assertTrue(all(r['uncertainty'] > 0 for r in board))
        self.ready('a', 'high')
        self.assertEqual(len(self.arena.leaderboard(task, 'model')), 3)

    def test_judgments_validate_benchmark_and_readiness(self):
        a, b = self.ready(), self.enqueue('b')
        with self.assertRaisesRegex(ValueError, 'ready websites'):
            self.arena.judge(self.task['id'], a['id'], b['id'], 'left')
        other = self.arena.create({'title': 'Other', 'prompt': 'Other brief'})
        c = self.ready(task=other)
        with self.assertRaises(ValueError): self.arena.judge(self.task['id'], a['id'], c['id'], 'tie')
        with self.assertRaises(ValueError): self.arena.judge(self.task['id'], a['id'], a['id'], 'left')

    def test_aggregate_requires_common_opponents_and_every_dated_run(self):
        a, b = self.ready('a'), self.ready('b')
        other = self.arena.create({'title':'Second task', 'prompt':'Another brief'})
        c, d = self.ready('a', task=other), self.ready('b', task=other)
        self.ready('only-one-task')
        keys = [self.task['id'], other['id']]
        result = self.arena.aggregate(keys)
        self.assertEqual(len(result['rows']), 2)
        self.assertTrue(all(r['score'] is None for r in result['rows']))
        self.arena.judge(keys[0], a['id'], b['id'], 'left')
        self.arena.judge(keys[1], c['id'], d['id'], 'tie')
        result = self.arena.aggregate(keys)
        self.assertEqual([r['score'] for r in result['rows']], [.75, .25])
        rerun = self.ready('a')
        result = self.arena.aggregate(keys)
        self.assertIsNone(next(r for r in result['rows'] if ' / a / ' in r['identity'])['score'])
        self.arena.judge(keys[0], rerun['id'], b['id'], 'right')
        result = self.arena.aggregate(keys)
        self.assertEqual([r['score'] for r in result['rows']], [.5, .5])

    def test_starter_archive_and_asset_paths_are_bounded_and_link_free(self):
        source = self.root / 'starter'; source.mkdir(); (source / 'index.html').write_text('starter')
        archive = self.root / 'starter.tar.gz'; pack(source, archive)
        task = self.arena.create({'title':'Starter', 'prompt':'Continue this site',
            'starter':base64.b64encode(archive.read_bytes()).decode()})
        self.assertEqual((self.arena.check_inputs(task) / 'starter/index.html').read_text(), 'starter')
        for name in ('../private', '/private', 'a/b', '.git'):
            with self.assertRaises(ValueError):
                self.arena.create({'title':'Bad', 'prompt':'Bad asset', 'assets':[{'name':name,'data':'eA=='}]})

    def test_normalize_links_preserves_npm_bin_target_and_rejects_external_links(self):
        root = self.root / 'node'; (root/'node_modules/.bin').mkdir(parents=True)
        script = root/'node_modules/cli.js'; script.write_text('#!/usr/bin/env node\n'); script.chmod(0o755)
        (root/'node_modules/.bin/cli').symlink_to('../cli.js')
        normalize_tree(root)
        wrapper = root/'node_modules/.bin/cli'
        self.assertFalse(wrapper.is_symlink())
        self.assertIn('/workspace/node_modules/cli.js', wrapper.read_text())
        (root/'bad').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError, 'external'): normalize_tree(root)

    def test_runtime_protocol_accepts_real_server_and_rejects_shell_strings(self):
        (self.root/'arena-runtime.json').write_text(json.dumps({'build':[['npm','ci'],['npm','run','build']], 'start':['node','server.js']}))
        self.assertEqual(runtime_config(self.root)['start'], ['node','server.js'])
        (self.root/'arena-runtime.json').write_text('{"start":"npm start"}')
        with self.assertRaises(ValueError): runtime_config(self.root)

    def test_codex_staging_uses_arena_schema_effort_and_disposable_mounts(self):
        from contextlib import contextmanager
        arena=self.arena
        class Sandbox:
            @contextmanager
            def container(inner, spec):
                assert arena.root not in spec.workspace.parents
                if spec.inputs:
                    schema=json.loads((spec.inputs/'schema.json').read_text())
                    self.assertEqual(schema['required'],['summary'])
                    self.assertNotIn('historical_solution_seen',(spec.inputs/'prompt.txt').read_text())
                    self.assertEqual(spec.command[-1],'model_reasoning_effort="high"')
                    self.assertIn('-c "$2"',spec.command[2])
                    (spec.control/'final.json').write_text('{"summary":"Fake Codex staging response"}')
                    (spec.workspace/'index.html').write_text('<h1>Website</h1>')
                else:
                    self.assertEqual(spec.network,'none')
                class Container:
                    image_id='sha256:staging-fixture'
                    def start(inner): pass
                    def wait(inner, timeout): return False
                    def inspect(inner): return {'Running':False,'ExitCode':0}
                    def exec(inner, argv, timeout):
                        return {'exit_code':0,'stdout':'{"status":200,"headers":[],"body":""}'}
                yield Container()
        r=arena.enqueue(self.task['id'],[dict(harness='codex',model='explicit-model',effort='high')])[0]
        result=Runner(arena,'fixture',sandbox=Sandbox()).generate(r['id'])
        self.assertEqual(result['status'],'ready',result)
        self.assertEqual(result['completion']['summary'],'Fake Codex staging response')
        self.assertEqual(result['config'],dict(harness='codex',model='explicit-model',effort='high'))

    def test_export_requires_both_privacy_reviews_before_browser_or_capture(self):
        from mineval.arena.sharing import export
        with self.assertRaisesRegex(ValueError, 'Review website'):
            export(self.arena, None, self.task['id'], {'reviewed_content':True})

    def test_cli_create_list_and_show_have_successful_exit_codes(self):
        from contextlib import redirect_stdout
        from mineval.cli import main
        definition=self.root/'brief.json';definition.write_text('{"title":"CLI brief","prompt":"Build a site"}')
        args=['--data',str(self.root/'cli-store'),'arena']
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args+['create',str(definition)]),0)
        key=json.loads(output.getvalue())['id']
        for command in (['list'],['show',key]):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(args+command),0)
                self.assertTrue(json.loads(output.getvalue()))

    def test_http_origin_token_host_and_historical_boundaries(self):
        app = App(self.arena, None, None)
        with make_server(app) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
            self.addCleanup(thread.join, 2);self.addCleanup(server.shutdown)
            url = urlsplit(server.arena_url)
            conn = http.client.HTTPConnection(url.hostname, url.port)
            self.addCleanup(conn.close)
            def request(method, path, data=None, headers=None):
                conn.request(method, path, json.dumps(data) if data is not None else None, headers or {})
                response=conn.getresponse();body=response.read();return response,body
            self.assertEqual(request('GET','/')[0].status,404)
            self.assertEqual(request('GET',url.path,headers={'Host':'evil.example'})[0].status,404)
            self.assertEqual(request('GET',url.path)[0].status,200)
            self.assertEqual(request('GET',url.path+'history/')[0].status,200)
            self.assertEqual(request('POST',url.path+'api/benchmarks',{'title':'x','prompt':'x'})[0].status,403)
            headers={'Origin':f'http://{url.netloc}','X-Arena-Request':'1','Content-Type':'application/json'}
            response,body=request('POST',url.path+'api/benchmarks',{'title':'x','prompt':'x'},headers)
            self.assertEqual(response.status,200)
            self.assertIn('id',json.loads(body))
            response,_=request('POST',url.path+'history/tasks/x/retire',{},headers)
            self.assertEqual(response.status,400)
            response,_=request('GET',url.path+'api/benchmarks')
            self.assertEqual(response.getheader('Cache-Control'),'no-store')
            self.assertIn("default-src 'none'",response.getheader('Content-Security-Policy'))


@unittest.skipUnless(os.environ.get('MINEVAL_ARENA_TEST_IMAGE'), 'set MINEVAL_ARENA_TEST_IMAGE for rootless fixture integration')
class LiveArenaTests(ArenaTests):
    def test_fixture_generation_interaction_proxy_and_immutable_reopen(self):
        r = self.enqueue()
        runner=Runner(self.arena,os.environ['MINEVAL_ARENA_TEST_IMAGE'],os.environ.get('MINEVAL_TEST_CONTEXT','mineval-rootless'))
        result=runner.generate(r['id']);self.assertEqual(result['status'],'ready',result)
        archive=self.arena.artifact(r['id']);before=digest(archive)
        manager=RuntimeManager(self.arena,runner.sandbox)
        self.addCleanup(manager.close)
        url=urlsplit(manager.open(r['id']))
        conn=http.client.HTTPConnection(url.hostname,url.port)
        conn.request('GET',url.path)
        response=conn.getresponse();response.read();cookie=response.getheader('Set-Cookie').split(';')[0]
        self.assertEqual(response.status,303)
        conn.request('GET','/',headers={'Cookie':cookie})
        response=conn.getresponse();body=response.read().decode()
        self.assertEqual(response.status,200);self.assertIn('Say hello',body)
        conn.request('GET','/explore',headers={'Cookie':cookie})
        response=conn.getresponse();response.read();self.assertEqual(response.status,200)
        conn.request('GET','/')
        response=conn.getresponse();response.read();self.assertEqual(response.status,404)
        preview=next(iter(manager.previews.values()))
        self.assertEqual(preview.container._cli.json('inspect',preview.container.id)[0]['HostConfig']['NetworkMode'],'none')
        manager.close()
        self.assertEqual(digest(archive),before)
        reopened_url = manager.open(r['id'])
        self.assertTrue(reopened_url)
        self.assertEqual(digest(archive),before)
        crashed = manager.previews[r['id']]
        crashed.container._cli.call('kill', crashed.container.id)
        self.assertNotEqual(manager.open(r['id']), reopened_url)
        self.assertEqual(digest(archive),before)

    def test_real_dynamic_http_build_post_and_per_runtime_cookies(self):
        starter = self.root / 'dynamic'; starter.mkdir()
        (starter / 'server.py').write_text("""from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  body=(Path('built.txt').read_text()+' '+self.headers.get('Cookie','')).encode()
  self.send_response(200)
  if self.path=='/set': self.send_header('Set-Cookie','session=private; Path=/; HttpOnly')
  self.end_headers();self.wfile.write(body)
 def do_POST(self):
  body=self.rfile.read(int(self.headers.get('Content-Length',0)))
  self.send_response(200);self.end_headers();self.wfile.write(body)
HTTPServer(('0.0.0.0',3000),H).serve_forever()
""")
        (starter / 'arena-runtime.json').write_text(json.dumps({
            'build':[['python3','-c',"from pathlib import Path;Path('built.txt').write_text('built-server')"]],
            'start':['python3','server.py']}))
        archive=self.root/'dynamic.tar.gz';pack(starter,archive)
        task=self.arena.create({'title':'Dynamic server','prompt':'Build a dynamic app',
            'starter':base64.b64encode(archive.read_bytes()).decode()})
        r=self.enqueue(task=task)
        runner=Runner(self.arena,os.environ['MINEVAL_ARENA_TEST_IMAGE'],os.environ.get('MINEVAL_TEST_CONTEXT','mineval-rootless'))
        result=runner.generate(r['id']);self.assertEqual(result['status'],'ready',result)
        self.assertEqual(result['build_results'][0]['exit_code'],0)
        manager=RuntimeManager(self.arena,runner.sandbox);self.addCleanup(manager.close)
        url=urlsplit(manager.open(r['id']));conn=http.client.HTTPConnection(url.hostname,url.port)
        self.addCleanup(conn.close)
        conn.request('GET',url.path);response=conn.getresponse();response.read()
        auth=response.getheader('Set-Cookie').split(';')[0]
        conn.request('GET','/set',headers={'Cookie':auth});response=conn.getresponse()
        self.assertIn(b'built-server',response.read());app_cookie=response.getheader('Set-Cookie').split(';')[0]
        conn.request('GET','/cookies',headers={'Cookie':auth+'; '+app_cookie+'; arena_app_other_session=foreign'})
        response=conn.getresponse();body=response.read().decode()
        self.assertIn('session=private',body);self.assertNotIn('arena_preview',body);self.assertNotIn('foreign',body)
        conn.request('POST','/save',body=b'user typed this',headers={'Cookie':auth,'Content-Type':'text/plain'})
        response=conn.getresponse();self.assertEqual(response.read(),b'user typed this')

    @unittest.skipUnless(os.environ.get('MINEVAL_ARENA_CAPTURE_TESTS'), 'optional real PNG/GIF capture checks')
    def test_real_png_and_animated_gif_exports(self):
        from PIL import Image
        from mineval.arena.sharing import export
        r=self.enqueue()
        runner=Runner(self.arena,os.environ['MINEVAL_ARENA_TEST_IMAGE'],os.environ.get('MINEVAL_TEST_CONTEXT','mineval-rootless'))
        result=runner.generate(r['id']);self.assertEqual(result['status'],'ready',result)
        manager=RuntimeManager(self.arena,runner.sandbox);self.addCleanup(manager.close)
        for kind in ('png','gif'):
            data=dict(generations=[r['id']],format=kind,anonymous=True,include_title=False,
                      include_prompt=False,reviewed_content=True,reviewed_identity=True)
            record=export(self.arena,manager,self.task['id'],data)
            path=self.arena.root/'shares'/(record['id']+'.'+kind)
            with Image.open(path) as image:
                self.assertEqual(image.width,1200)
                self.assertEqual(getattr(image,'n_frames',1),3 if kind=='gif' else 1)
            self.assertEqual(digest(path),record['sha256'])
            self.assertEqual(record['options'],data)
