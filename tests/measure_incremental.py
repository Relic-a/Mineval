"""Reproducible local measurements, no model calls, downloads, pulls or image builds.

Run: python tests/measure_incremental.py --context ROOTLESS --image PRELOADED_PYTHON_GIT_IMAGE
The image must contain python3, pip and Git. Uses an offline synthetic hashed wheel
and fixed fake benchmark adapters. Reports timings; does not assert speed thresholds.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mineval.artifacts import copy_tree, write_json
from mineval.dependency_cache import DependencyCache
from mineval.sandbox import DockerSandbox, Limits, SandboxSpec
from mineval.workflows import Workflows
from test_preparation import HistoryFixture, FakeRunner, step
from mineval.verification import diagnose


def timed(action):
    start = time.perf_counter()
    value = action()
    return value, round(time.perf_counter() - start, 4)


def measure(context, image):
    result = {}
    with tempfile.TemporaryDirectory(prefix='mineval-measure-') as temp:
        root = Path(temp)
        tree = root / 'historical'; tree.mkdir()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('demo.py', 'ANSWER = 42\n')
            archive.writestr('demo-1.0.dist-info/METADATA', 'Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n')
            archive.writestr('demo-1.0.dist-info/WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
            archive.writestr('demo-1.0.dist-info/RECORD', '')
        data = buffer.getvalue(); checksum = hashlib.sha256(data).hexdigest()
        (tree / 'requirements.txt').write_text(f'demo==1.0 --hash=sha256:{checksum}\n')
        url = 'https://files.pythonhosted.org/packages/demo-1.0-py3-none-any.whl'
        responses = {url: data, 'https://pypi.org/pypi/demo/1.0/json': json.dumps({'urls': [
            {'url': url, 'digests': {'sha256': checksum}, 'packagetype': 'bdist_wheel'}]}).encode()}
        cache = DependencyCache(root / 'cache', tree)
        sandbox = DockerSandbox(context=context)
        sandbox.check(); runtime = sandbox.image(image)
        with patch('mineval.dependency_cache.download', side_effect=lambda url, *a: responses[url]) as fetch:
            for name in ('cold', 'warm'):
                (local, stats), seed_seconds = timed(lambda: cache.prepare(('pip',), network='bridge'))
                _, install_seconds = timed(lambda: cache.pip_environment(sandbox, runtime, local, stats, Limits()))
                workspace = root / name; copy_tree(tree, workspace)
                command = ('sh', '-c', 'python3 -m pip install --no-index --disable-pip-version-check --require-hashes -r requirements.txt && python3 -c "import demo; assert demo.ANSWER == 42"')
                spec = SandboxSpec(runtime['Id'], command, workspace=workspace, network='none')
                def execute():
                    with sandbox._container(spec, image=runtime, dependency_cache_path=local, cache_managers=('pip',)) as container:
                        container.start(); deadline = container.wait(60)
                        return container.result(timed_out=deadline, duration_seconds=0)
                checked, execution_seconds = timed(execute)
                if checked.exit_code or 'Requirement already satisfied' not in checked.stdout:
                    raise RuntimeError('Installed environment not used: ' + checked.stderr + checked.stdout)
                result['pip_' + name] = {'seed_seconds': seed_seconds, 'environment_seconds': install_seconds,
                    'sandbox_check_seconds': execution_seconds, 'environment': stats.get('installed_environment'),
                    'native_install_skipped': 'Requirement already satisfied' in checked.stdout}
                # Mutate the task-private environment: it must not affect the next one.
                (local / 'pip/site-packages/demo.py').write_text('raise RuntimeError("poisoned task")\n')
                shutil.rmtree(local)
            result['host_download_calls'] = fetch.call_count
        fixture = HistoryFixture()
        fixture.setUp()
        try:
            config = {'image': image, 'network': 'none', 'commands': [
                {'name': 'regression', 'kind': 'tests', 'argv': ['sh', '-c', 'PYTHONPATH=. python3 tests/test_answer.py']}]}
            write_json(fixture.config_path, config)
            diagnose(fixture.store, 'example', sandbox=FakeRunner([step()]))
            version = fixture.freeze('example', fixture.review)['version']
            for name in ('cold', 'warm'):
                _, elapsed = timed(lambda: fixture.store.materialize('example', version, root / ('materialized-' + name)))
                result['materialize_' + name + '_seconds'] = elapsed
            evaluation_config = root / 'evaluation.json'; write_json(evaluation_config, config)
            for name, limits in [('serial', {'total': 1}), ('bounded', {'total': 4, 'benchmark': 2, 'evaluation': 1, 'review': 1})]:
                flow = Workflows(fixture.store, {'evaluation_config': str(evaluation_config), 'concurrency': limits}, context=context)
                flow.enqueue('benchmark', [{'task_id': 'example', 'version': version}],
                    [{'model': name + str(i), 'adapter': 'fake'} for i in range(4)], exclude=False)
                _, elapsed = timed(flow.work)
                jobs = [j for j in flow.jobs() if j['payload']['configuration']['model'].startswith(name)]
                if {j['state'] for j in jobs} != {'completed'}:
                    raise RuntimeError(str(jobs))
                result['batch_' + name] = {'seconds': elapsed, 'attempts': len(jobs),
                    'evaluations': len(jobs), 'model_calls': 0, 'publication': 'all completed'}
        finally:
            fixture.doCleanups()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    print(json.dumps(measure(args.context, args.image), indent=2))
