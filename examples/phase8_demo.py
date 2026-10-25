"""Small actual-Git/rootless/fake CLI walkthrough. Never calls a coding model.

Run from the checkout: python -m examples.phase8_demo --directory /tmp/mineval-demo
    --context mineval-rootless --image mineval-fixture:phase4
The directory must be new. Its persistent store is suitable for `mineval observe`.
"""
import argparse
import http.client
import json
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import urlsplit

from mineval.artifacts import inventory, read_json, write_json
from mineval.observation import make_server
from mineval.preparation import Store


def demonstrate(directory, context, image):
    directory = Path(directory).absolute()
    directory.mkdir(parents=True, exist_ok=False)
    repo = directory / 'source'; repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', '-C', str(repo), *args], stderr=subprocess.PIPE).decode().strip()
    git('init', '-q')
    (repo / 'app.py').write_text('def answer(): return 0\n')
    git('add', '.'); git('commit', '-qm', 'Introduce answer function')
    past = git('rev-parse', 'HEAD')
    (repo / 'README.md').write_text('The answer function must return the answer to life: 42.\n')
    git('add', '.'); git('commit', '-qm', 'Document required answer')
    base = git('rev-parse', 'HEAD')
    (repo / 'app.py').write_text('def answer(): return 42\n')
    (repo / 'tests').mkdir()
    (repo / 'tests/test_answer.py').write_text('from app import answer\nassert answer() == 42\n')
    git('add', '.'); git('commit', '-qm', 'Correct answer and regress it')
    head = git('rev-parse', 'HEAD')
    candidate = {'task_id': 'answer', 'repository': 'local-fixture', 'base_commit': base, 'head_commit': head,
        'task_summary': 'Make answer() return the documented answer to life, including repeated calls.',
        'base_source': {'type': 'git', 'path': str(repo)}, 'related_commits': [],
        'why_high_signal': 'Deterministic integration fixture; deliberately trivial, not a quality benchmark.',
        'future_evidence': [{'commit': head, 'path': 'tests/test_answer.py', 'proves': 'The documented answer is returned'}],
        'execution': {'runtime': 'Python 3', 'likely_commands': ['python3 tests/test_answer.py'],
                      'external_requirements': [], 'cleanup_risks': []},
        'verification_feasibility': 'One actual historical assertion checks the required value.',
        'uncertainty': [], 'selection_status': 'accept'}
    write_json(directory / 'candidate.json', candidate)
    data = directory / 'data'
    base_cli = [sys.executable, '-m', 'mineval', '--context', context, '--data', str(data)]
    def cli(*args, expected=0):
        result = subprocess.run([*base_cli, *args], capture_output=True, text=True, timeout=180)
        if result.returncode != expected:
            raise RuntimeError(f'{args[0]} exited {result.returncode}: {result.stderr}\n{result.stdout}')
        return json.loads(result.stdout)
    cli('task', 'add', str(directory / 'candidate.json'))
    config = {'image': image, 'network': 'none', 'limits': {'timeout_seconds': 60}, 'commands': [
        {'name': 'past-history', 'kind': 'smoke', 'argv': ['sh', '-c', 'git log --oneline && git blame app.py && python3 -c "import app"']}]}
    write_json(data / 'drafts/answer/control/verify.json', config)
    # The fake cleaner invokes verify through the broker in a new disposable container.
    cli('cleanup', 'answer', '--adapter', 'fake')
    diagnostic = cli('verify', '--task', 'answer')
    assert diagnostic['status'] == 'completed'
    plan = {'strategy': 'focused', 'rationale': 'Check the documented return value without prescribing its implementation',
        'dependencies': [], 'instructions': 'PYTHONPATH=. python3 tests/test_answer.py',
        'limitations': ['Synthetic trivial task; not representative of model quality'],
        'artifacts': [{'commit': head, 'path': 'tests/test_answer.py', 'purpose': 'Return value regression', 'test_only': True}]}
    def producer(filename, value):
        return ['python3', '-c', 'import pathlib; pathlib.Path(' + repr('/mineval-control/' + filename) + ').write_text(' + repr(json.dumps(value)) + ')']
    extracted = cli('verification-extract', 'answer', '--command', *producer('selection.json', plan))
    checks = directory / 'evaluation.json'
    write_json(checks, {'image': image, 'network': 'none', 'commands': [
        {'name': 'historical-answer', 'kind': 'tests', 'argv': ['sh', '-c', 'PYTHONPATH=. python3 tests/test_answer.py']}]})
    cli('verification-check', 'answer', '--extraction-id', extracted['extraction_id'], '--config', str(checks), expected=1)
    cli('verification-check', 'answer', '--extraction-id', extracted['extraction_id'], '--config', str(checks), '--future-commit', head)
    review = {'verdict': 'approve', 'objective_preserved': True, 'snapshot_reviewed': True, 'changes': [],
              'reasons': ['Fixture review: no cleanup changes; focused future assertion; no future files in starting tree']}
    approved = cli('review', 'answer', '--extraction-id', extracted['extraction_id'], '--command', *producer('review.json', review))
    version = cli('finalize', 'answer', '--review-id', approved['review_id'])['version']
    frozen_before = inventory(data / 'tasks/answer/versions')
    workspace = directory / 'historical'
    cli('workspace', 'answer', version, str(workspace))
    def historical(*args):
        return subprocess.run(['git', '-C', str(workspace), *args], capture_output=True, text=True)
    assert set(historical('rev-list', '--all').stdout.splitlines()) == {past, base}
    assert historical('blame', 'app.py').returncode == 0
    assert historical('cat-file', '-e', head).returncode != 0
    assert not (workspace / 'tests').exists() and not (workspace / 'private').exists()
    suite = directory / 'suite.json'
    write_json(suite, {'suite_id': 'fixture', 'members': [{'task_id': 'answer', 'version': version}]})
    cli('suite', 'create', str(suite))
    suite_before = cli('suite', 'show', 'fixture')
    run = cli('bench', 'run', '--suite', 'fixture', '--run-id', 'fake', '--adapter', 'fake')[0]
    assert run['status'] == 'completed'
    run_id = run['run_id']
    # The fake harness intentionally leaves the bug unfixed. Completion is not success.
    assessment = cli('evaluate', run_id, '--task', 'answer', '--config', str(checks), '--evaluation-id', 'initial', expected=1)
    assert assessment['verdict'] == 'fail'
    outputs_before = inventory(data / 'tasks/answer/run-outputs')
    write_json(directory / 'deferred.json', candidate | {'task_id': 'deferred', 'selection_status': 'defer',
                                                        'uncertainty': ['Not yet ready for benchmarking']})
    cli('task', 'add', str(directory / 'deferred.json'))
    cli('task', 'retire', 'answer', '--reason', 'Fixture completed; exclude from future benchmarking')
    assert cli('suite', 'show', 'fixture') == suite_before
    assert cli('bench', 'show', 'answer', run_id) == run
    assert cli('evaluation', 'show', 'answer', 'initial') == assessment
    assert inventory(data / 'tasks/answer/versions') == frozen_before
    assert inventory(data / 'tasks/answer/run-outputs') == outputs_before
    blocked = subprocess.run([*base_cli, 'bench', 'run', '--suite', 'fixture', '--run-id', 'blocked'], capture_output=True, text=True)
    assert blocked.returncode == 125 and 'retired' in blocked.stderr
    # Reopen the store and inspect through real HTTP, including private evidence and diffs.
    with make_server(Store(data)) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        prefix = urlsplit(server.observation_url).path.rstrip('/')
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
        routes = ['/', '/tasks/answer', '/suites/fixture', '/tasks/answer/revisions/' + version,
                  '/tasks/answer/runs/' + run_id, '/tasks/answer/evaluations/initial',
                  '/tasks/answer/artifacts/evaluations/initial?path=baseline/selected-checks.json',
                  '/compare?kind=evaluations&left=answer:initial&right=answer:initial']
        try:
            for route in routes:
                connection.request('GET', prefix + route)
                response = connection.getresponse(); content = response.read().decode()
                assert response.status == 200, (route, content)
                if 'selected-checks' in route:
                    assert head in content
                if route.endswith('/runs/' + run_id):
                    assert 'mineval-fake-result.txt' in content
        finally:
            connection.close(); server.shutdown(); thread.join()
    summary = {'store': str(data), 'task': 'answer', 'version': version, 'run_id': run_id,
        'evaluation_id': 'initial', 'verdict': 'fail', 'ui_routes_checked': len(routes),
        'historical_commits': [past, base], 'excluded_future_commit': head,
        'retired': True, 'frozen_and_output_artifacts_unchanged': True,
        'limitations': ['Fake agents and scripted independent reviewer/extractor; no live Codex or model-quality validation']}
    write_json(directory / 'summary.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, help='new persistent demo directory outside your store')
    parser.add_argument('--context', default='mineval-rootless')
    parser.add_argument('--image', default='mineval-fixture:phase4', help='trusted preloaded image with Python, Git and Mineval')
    args = parser.parse_args()
    print(json.dumps(demonstrate(args.directory, args.context, args.image), indent=2))
