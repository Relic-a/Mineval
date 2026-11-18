from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from mineval import evaluation, http_grading
from mineval.artifacts import write_json
from mineval.verification import load_config


class LocalRunner:
    """Exercise a real HTTP service and transport locally, not a security substitute for Docker."""
    def __init__(self): self.calls = []; self.process = None
    @contextmanager
    def container(self, spec):
        self.spec = spec
        container = Mock(id='local', image_id='local-image')
        container.inspect.return_value = {'Running': True}
        def execute(argv, **kwargs):
            self.calls.append(argv)
            if len(argv) > 2 and argv[2] == http_grading.START:
                self.process = subprocess.Popen(['python3', 'server.py'], cwd=spec.workspace, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                result = subprocess.CompletedProcess(argv, 0, '', '')
            else:
                result = subprocess.run(argv, cwd=spec.workspace, capture_output=True, text=True, timeout=kwargs['timeout'])
            return {'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
                    'timed_out': False, 'oom_killed': False, 'logs_truncated': False}
        container.exec.side_effect = execute
        try: yield container
        finally:
            if self.process:
                self.process.terminate(); self.process.wait(timeout=5)


class HttpGradingTests(unittest.TestCase):
    def test_stateful_http_real_process_and_private_expectations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source'; source.mkdir(); private = root / 'private'; private.mkdir()
            sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]; sock.close()
            (source / 'server.py').write_text('''from http.server import BaseHTTPRequestHandler,HTTPServer
class App(BaseHTTPRequestHandler):
 value=0
 def do_POST(self):
  App.value+=1; self.send_response(201); self.send_header('X-Middleware','on'); self.end_headers(); self.wfile.write(str(App.value).encode())
 def do_GET(self):
  self.send_response(200); self.end_headers(); self.wfile.write(str(App.value).encode())
 def log_message(self,*args): pass
HTTPServer(('127.0.0.1',%d),App).serve_forever()
''' % port)
            write_json(private / 'cases.json', {'cases': [{'name': 'state and middleware', 'steps': [
                {'method':'POST','path':'/items','expect':{'status':201,'body':'1','headers':{'x-middleware':'on'}}},
                {'method':'GET','path':'/items','expect':{'status':200,'body':'1'}}]}]})
            config = {'image':'local:test','network':'none','grading_type':'isolated-http-v1',
                      'service':{'argv':['python3','server.py'],'port':port},
                      'commands':[{'name':'http','kind':'tests','argv':['true']}]}
            write_json(root / 'config.json', config); self.assertEqual(load_config(root / 'config.json', evaluation=True), config)
            runner = LocalRunner()
            report = evaluation.execute_checks(source, root / 'workspace', root / 'results', private,
                [{'path':'cases.json','stored_path':'cases.json'}], [], config, config['image'], runner)
            self.assertEqual(report['verdict'], 'pass', report)
            self.assertEqual(len(report['cases'][0]['steps']), 2)
            self.assertEqual(runner.spec.network, 'none')
            self.assertIsNone(runner.spec.inputs); self.assertIsNone(runner.spec.control)
            self.assertFalse((runner.spec.workspace / 'cases.json').exists())
            self.assertNotIn('x-middleware', str(runner.calls).lower())
            self.assertNotIn('201', str(runner.calls))
            sock = socket.socket(); sock.bind(('127.0.0.1', 0)); second_port = sock.getsockname()[1]; sock.close()
            (source / 'server.py').write_text((source / 'server.py').read_text().replace(str(port), str(second_port)))
            config['service']['port'] = second_port
            checks = json.loads((private / 'cases.json').read_text())
            checks['cases'][0]['steps'][0]['expect']['status'] = 202
            write_json(private / 'cases.json', checks)
            failed = evaluation.execute_checks(source, root / 'workspace2', root / 'results2', private,
                [{'path':'cases.json','stored_path':'cases.json'}], [], config, config['image'], LocalRunner())
            self.assertEqual(failed['verdict'], 'fail', failed)
            self.assertEqual(len(failed['cases'][0]['steps']), 2)
            self.assertEqual(failed['cases'][0]['steps'][0]['mismatches'], ['status'])

    def test_invalid_or_empty_cases_fail_closed(self):
        for document in ({'cases': []}, {'cases':[{'name':'skipped','steps':[]}]}, {'cases':[{'name':'x','steps':[{'method':'GET','path':'//evil','expect':{'status':200}}]}]}):
            with self.subTest(document=document), self.assertRaises(ValueError): http_grading._document(document)

from test_preparation import HistoryFixture

class GeneratedChecksTests(HistoryFixture):
    def test_generated_and_historical_evidence_are_sealed_together(self):
        generated = {'origin': 'generated', 'path': 'checks/http.json', 'purpose': 'HTTP behavior',
                     'test_only': True, 'content': json.dumps({'cases': [{'name': 'route', 'steps': [
                         {'method': 'GET', 'path': '/health', 'expect': {'status': 200}}]}]})}
        historical = {'commit': self.head, 'path': 'tests/test_answer.py', 'purpose': 'Historical regression', 'test_only': True}
        with self.store.operation('example'):
            result = self.store._extract('example', [generated, historical], plan={'origin': 'fixture'}, producer_session={'session_id': 'fixture'})
        root = self.store.task('example') / 'private/extractions' / result['extraction_id']
        from mineval.review import checked
        checked(root)
        self.assertEqual(len(result['artifacts']), 2)
        self.assertNotIn('content', result['artifacts'][0])
        self.assertEqual((root / result['artifacts'][0]['stored_path']).read_text(), generated['content'])
        self.assertEqual((root / result['artifacts'][1]['stored_path']).read_text(), (self.repo / 'tests/test_answer.py').read_text())

class PreparationHttpTests(HistoryFixture):
    def test_generated_checks_validate_base_and_reference_then_freeze(self):
        from mineval import review
        from review_helpers import ReportAgent, report
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]; sock.close()
        server = '''from http.server import BaseHTTPRequestHandler,HTTPServer
from app import answer
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200); self.end_headers(); self.wfile.write(str(answer()).encode())
 def log_message(self,*args): pass
class Server(HTTPServer): allow_reuse_address=True
Server(('127.0.0.1',%d),Handler).serve_forever()
''' % port
        self.prepare_review('example', self.review)
        config = {'image':'local:test','network':'none','grading_type':'isolated-http-v1',
                  'service':{'argv':['python3','server.py'], 'port':port}, 'commands':[
                      {'name':'test-harness','kind':'setup','argv':['python3','-c', 'from pathlib import Path; Path("server.py").write_text(%r)' % server]},
                      {'name':'http','kind':'tests','argv':['true']}]}
        artifact = {'origin':'generated','path':'checks/http.json','purpose':'Answer behavior','test_only':True,
                    'content':json.dumps({'cases':[{'name':'answer','steps':[{'method':'GET','path':'/','expect':{'status':200,'body':'42'}}]}]})}
        with self.store.operation('example'):
            extraction = self.store._extract('example', [artifact], plan={'evaluation_config':config}, producer_session={'session_id':'fixture'})
        path = self.root / 'http-config.json'; write_json(path, config)
        base = review.validate(self.store, 'example', extraction['extraction_id'], path, sandbox=LocalRunner())
        future = review.validate(self.store, 'example', extraction['extraction_id'], path, future_commit=self.head, sandbox=LocalRunner())
        self.assertEqual(base['report']['verdict'], 'fail', base)
        self.assertEqual(future['report']['verdict'], 'pass', future)
        approval = review.session(self.store, 'example', stage='review', command=['fixture'],
            extraction_id=extraction['extraction_id'], sandbox=ReportAgent('review.json', report(self.review)))
        frozen = self.store.finalize('example', review_id=approval['review_id'])
        version = self.store.task('example') / 'versions' / frozen['version']
        self.assertEqual(json.loads((version / 'evaluation-config.json').read_text()), config)
        from contextlib import contextmanager
        from unittest.mock import patch
        from mineval import architecture, benchmark
        from mineval.agents import FakeAdapter
        from mineval.observation import Pages
        from test_agents import SimulatedDocker
        class Candidate(SimulatedDocker):
            def __init__(self, code): super().__init__(); self.candidate_code = code
            @contextmanager
            def container(self, spec):
                with super().container(spec) as container:
                    start = container.start.side_effect
                    def save():
                        start(); (spec.workspace / 'app.py').write_text(self.candidate_code)
                    container.start.side_effect = save
                    yield container
        def infer(store, workspace, prompt, **options):
            if prompt == architecture.RECORD_PROMPT:
                return {'observations':[{'description':'Function returns the answer','evidence':['app.py']}]}
            labels = sorted(p.name for p in (workspace / 'candidates').iterdir())
            return {'ranking':[{'candidates':labels,'explanation':'Equivalent behavior',
                'evidence':[{'candidate':label,'path':'app.py'} for label in labels]}], 'uncertainty':'Similar approaches'}
        options = {'image':'local:test','model':'reviewer','effort':'low'}
        with patch('mineval.architecture._infer', side_effect=infer):
            for name, code in [('one','def answer(): return 42\n'), ('two','def answer(): return 6 * 7\n')]:
                benchmark.run(self.store, 'example', frozen['version'], run_id=name,
                              adapter=FakeAdapter(model=name), sandbox=Candidate(code))
                assessment = evaluation.evaluate(self.store, 'example', name, path, evaluation_id='assessment-'+name, sandbox=LocalRunner())
                self.assertEqual(assessment['verdict'], 'pass', assessment)
                architecture.record(self.store, 'example', name, **options)
                comparison = architecture.compare(self.store, 'example', frozen['version'], **options)
            altered = dict(config, service={'argv': ['python3', 'server.py'], 'port': port + 1 if port < 65535 else port - 1})
            altered_path = self.root / 'altered.json'; write_json(altered_path, altered)
            with self.assertRaisesRegex(ValueError, 'differs from the frozen'):
                evaluation.evaluate(self.store, 'example', 'one', altered_path, sandbox=LocalRunner())
            self.assertEqual(len(comparison['candidates']), 2)
            page = Pages(self.store).task_page('example')
            self.assertIn('Architecture comparisons', page)
            self.assertIn('Equivalent behavior', page)


if __name__ == '__main__': unittest.main()
