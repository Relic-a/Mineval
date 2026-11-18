"""Human observation, HTTP boundaries and preservation after retirement."""
from contextlib import redirect_stdout
from html import unescape
import http.client
import io
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from mineval import benchmark
from mineval.artifacts import inventory, read_json, write_json
from mineval.cli import main
from mineval.observation import Pages, make_server
from mineval.preparation import Store
import test_evaluation as fixtures


class ObservationTests(fixtures.HistoryFixture):
    def setUp(self):
        super().setUp()
        self.fixture_generated_cases = True
        self.fixture_evaluation_config = {'image': 'local:test', 'network': 'none', 'grading_type': 'isolated-argv-v1',
            'commands': [{'name': 'answer-regression', 'kind': 'tests',
                          'argv': ['python3', '-c', 'from app import answer; print(answer())']}]}
        self.version = self.freeze('example', self.review)['version']
        self.config = self.root / 'evaluation.json'
        write_json(self.config, self.fixture_evaluation_config)

    saved = fixtures.EvaluationTests.saved
    assess = fixtures.EvaluationTests.assess

    def test_pages_link_real_provenance_outputs_evidence_and_comparisons(self):
        self.saved('left'); self.saved('right', 'def answer(): return 7\n')
        self.assess('left', 'pass'); self.assess('right', 'fail')
        self.store.create_suite({'suite_id': 'sample', 'members': [
            {'task_id': 'example', 'version': self.version}]})
        pages = Pages(self.store)
        for route, expected in [('/', 'Benchmark overview'), ('/tasks/example', 'Preparation history'),
                ('/suites', 'sample'), ('/suites/sample', self.version),
                ('/tasks/example/revisions/' + self.version, 'private/verification'),
                ('/tasks/example/runs/left', '+def answer(): return 42'),
                ('/tasks/example/evaluations/pass', 'Private evidence')]:
            content = pages.render(route)
            self.assertIn(expected, content)
        task = pages.render('/tasks/example')
        links = re.findall(r'href="([^"]+)"', task)
        for link in links:
            url = urlsplit(unescape(link))
            from urllib.parse import parse_qs
            pages.render(url.path, parse_qs(url.query))
        evidence = pages.render('/tasks/example/artifacts/evaluations/pass', {'path': ['baseline/selected-checks.json']})
        self.assertIn('generated/tests/cases.json', evidence)
        self.assertIn('executed_sha256', evidence)
        result = pages.render('/compare', {'kind': ['evaluations'], 'left': ['example:pass'], 'right': ['example:fail']})
        self.assertIn('1 exact common revisions', result)
        self.assertIn('pass_rate', result)
        self.assertIn('fake / fake', pages.render('/compare'))
        self.version = self.freeze('example', self.review)['version']
        self.saved('different-revision'); self.assess('different-revision', 'different')
        empty = pages.render('/compare', {'left': ['example:pass'], 'right': ['example:different']})
        self.assertIn('0 exact common revisions', empty)
        self.assertIn('Unavailable (no common tasks)', empty)
        with self.assertRaisesRegex(ValueError, 'Unsafe artifact'):
            pages.render('/tasks/example/artifacts/evaluations/pass', {'path': ['../../metadata.json']})
        path = self.store.task('example') / 'evaluation-evidence/pass/baseline/execution.json'
        path.chmod(0o644); path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            pages.render('/tasks/example/evaluations/pass')

    def test_http_is_private_readonly_escaped_and_reports_errors(self):
        self.saved(); self.assess(evaluation_id='http')
        self.saved('escaped', code='def answer(): return 42\n# </pre><script>alert(1)</script>\n')
        with make_server(self.store) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            self.addCleanup(thread.join, 2)
            self.addCleanup(server.shutdown)
            prefix = urlsplit(server.observation_url).path.rstrip('/')
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            self.addCleanup(connection.close)
            def get(path, headers=None):
                connection.request('GET', path, headers=headers or {})
                response = connection.getresponse()
                return response, response.read().decode()
            response, body = get(prefix + '/')
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            self.assertIn("default-src 'none'", response.getheader('Content-Security-Policy'))
            self.assertEqual(get('/')[0].status, 404)
            self.assertEqual(get(prefix + '/', {'Host': 'attacker.example'})[0].status, 404)
            connection.request('POST', prefix + '/tasks/example/retire')
            response = connection.getresponse(); response.read()
            self.assertEqual(response.status, 501)
            self.assertEqual(self.store.inspect_task('example')['state']['status'], 'frozen')
            response, body = get(prefix + '/tasks/example/runs/escaped')
            self.assertEqual(response.status, 200)
            self.assertNotIn('<script>', body)
            self.assertIn('&lt;script&gt;', body)
            response, body = get(prefix + '/tasks/example/artifacts/evaluations/http?path=%3Cscript%3E')
            self.assertEqual(response.status, 400)
            self.assertNotIn('<script>', body)
            response, body = get(prefix + '/compare?left=malformed')
            self.assertEqual(response.status, 400)
            self.assertIn('Unable to inspect', body)

    def test_retirement_blocks_new_work_preserves_suites_results_and_reevaluation(self):
        self.saved(); self.assess(evaluation_id='before')
        self.store.create_suite({'suite_id': 'stable', 'members': [
            {'task_id': 'example', 'version': self.version}]})
        # A retired later member must stop the whole suite before any earlier launch.
        from test_preparation import FakeRunner, step
        from mineval.verification import diagnose
        self.store.register(self.candidate | {'task_id': 'active'})
        write_json(self.store.draft('active') / 'control/verify.json', read_json(self.config_path))
        diagnose(self.store, 'active', sandbox=FakeRunner([step()]))
        active_version = self.freeze('active', self.review)['version']
        self.store.create_suite({'suite_id': 'mixed', 'members': [
            {'task_id': 'active', 'version': active_version},
            {'task_id': 'example', 'version': self.version}]})
        before = (self.store.inspect_run('example', 'candidate'), self.store.inspect_evaluation('example', 'before'),
                  self.store.inspect_suite('stable'), inventory(self.store.task('example') / 'versions'))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--data', str(self.store.root), 'task', 'retire', 'example', '--reason', 'Poor evidence']), 0)
        reopened = Store(self.store.root)
        self.assertEqual(reopened.inspect_task('example')['state']['status'], 'retired')
        self.assertEqual((reopened.inspect_run('example', 'candidate'), reopened.inspect_evaluation('example', 'before'),
                         reopened.inspect_suite('stable'), inventory(reopened.task('example') / 'versions')), before)
        for action in (lambda: benchmark.run(reopened, 'example', self.version, run_id='blocked'),
                lambda: benchmark.run_suite(reopened, 'stable', run_id='blocked'),
                lambda: benchmark.run_suite(reopened, 'mixed', run_id='blocked'),
                lambda: reopened.create_suite({'suite_id': 'blocked', 'members': [{'task_id': 'example', 'version': self.version}]})):
            with self.assertRaisesRegex(ValueError, 'retired'):
                action()
        self.assertFalse((reopened.task('example') / 'runs/blocked.json').exists())
        self.assertEqual(reopened.inspect_task('active')['runs'], [])
        self.assertEqual(self.assess(evaluation_id='after')['verdict'], 'pass')
        self.assertIn('retired', Pages(reopened).render('/tasks/example'))
        with self.assertRaisesRegex(ValueError, 'already retired'):
            reopened.retire('example', 'again')



@unittest.skipUnless(os.environ.get('MINEVAL_AGENT_TEST_IMAGE'), 'set MINEVAL_AGENT_TEST_IMAGE for live end-to-end check')
class LiveObservationTests(unittest.TestCase):
    def test_actual_history_cli_pipeline_and_http_inspection(self):
        from examples.phase8_demo import demonstrate
        with tempfile.TemporaryDirectory() as temp:
            summary = demonstrate(Path(temp) / 'demo', os.environ.get('MINEVAL_TEST_CONTEXT', 'mineval-rootless'),
                                  os.environ['MINEVAL_AGENT_TEST_IMAGE'])
            self.assertEqual(summary['verdict'], 'fail')
            self.assertEqual(summary['ui_routes_checked'], 8)
            self.assertTrue(summary['frozen_and_output_artifacts_unchanged'])
