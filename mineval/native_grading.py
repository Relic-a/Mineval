"""Run reviewed native tests in a disposable project sandbox."""
import shutil
import stat
import xml.etree.ElementTree as ET

from .artifacts import copy_tree, digest, relative, write_json
from .sandbox import Limits, SandboxSpec

REPORT = 'junit.xml'


def validate_config(config):
    script = relative(config.get('script', ''))
    if script.suffix != '.sh':
        raise ValueError('Verification entry point must be a .sh file')
    tests = config.get('required_tests')
    if (not isinstance(tests, list) or not tests or len(tests) > 10000
            or any(not isinstance(test, str) or not test.strip() for test in tests)
            or len(set(tests)) != len(tests)):
        raise ValueError('Declare unique nonempty required test IDs')


def script_artifact(artifacts, config):
    matches = [item for item in artifacts if item['path'] == config['script']]
    if len(matches) != 1:
        raise ValueError(f"Verification entry point must be one frozen regular file: {config['script']}")
    return matches[0]


def junit(path, required):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 8 * 1024 * 1024:
        raise ValueError('Invalid JUnit report file')
    root = ET.fromstring(path.read_bytes())
    if root.tag not in ('testsuite', 'testsuites'):
        raise ValueError('Expected a JUnit test suite')
    outcomes = {}
    for case in root.iter('testcase'):
        name = case.get('name')
        if not name:
            raise ValueError('JUnit testcase has no name')
        classname = case.get('classname')
        key = f'{classname}::{name}' if classname else name
        status = ('failed' if case.find('failure') is not None or case.find('error') is not None else
                  'skipped' if case.find('skipped') is not None else 'passed')
        if key in outcomes:
            raise ValueError(f'Duplicate JUnit testcase ID: {key}')
        outcomes[key] = status
    return {status: sorted(key for key, value in outcomes.items() if value == status)
            for status in ('passed', 'failed', 'skipped')} | {
                'missing': sorted(set(required) - outcomes.keys())}


def execute(source, workspace, staged, verification, artifacts, config, image, runner,
            *, prepare_workspace=None):
    staged.mkdir()
    report = {'commands': [], 'verdict': 'error', 'explanation': 'Native test setup or execution failed.'}
    try:
        script_artifact(artifacts, config)
        copy_tree(source, workspace)
        if prepare_workspace is not None:
            prepare_workspace(workspace)
        protected = []
        for item in artifacts:
            name = item['path']
            path = relative(name)
            frozen = verification / relative(item['stored_path'])
            if frozen.is_symlink() or not frozen.is_file() or digest(frozen) != item['sha256']:
                raise ValueError(f'Invalid frozen check: {name}')
            target = workspace
            for part in path.parts:
                target = target / part
                if target.is_symlink():
                    raise ValueError(f'Check overlay encounters a symbolic link: {name}')
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not target.is_file():
                raise ValueError(f'Check overlay requires a regular file: {name}')
            shutil.copyfile(frozen, target)
            protected.append(name)
        write_json(staged / 'selected-checks.json', [
            item | {'executed_sha256': item['sha256'], 'adapted': False} for item in artifacts])
        control = staged.parent / (staged.name + '-native-output')
        control.mkdir()
        limits = Limits(**config.get('limits', {}))
        argv = ('/bin/sh', '/workspace/' + config['script'])
        spec = SandboxSpec(image, ('/bin/sh', '-c', 'while :; do sleep 1; done'), limits,
                           workspace=workspace, workdir=config.get('workdir', '/workspace'),
                           network=config.get('network', 'none'), control=control,
                           readonly_paths=tuple(protected), services=config.get('services', ()),
                           environment={'MINEVAL_TEST_REPORT': '/mineval-control/' + REPORT})
        try:
            with runner.container(spec) as container:
                container.start()
                report.update(image_id=container.image_id, container_id=container.id)
                service_images = getattr(container, 'service_images', None)
                if isinstance(service_images, dict):
                    report['service_images'] = service_images
                result = container.exec(argv, timeout=limits.timeout_seconds, workdir=spec.workdir)
                report['commands'].append({'name': config['script'], 'kind': 'tests', **result})
                if result['timed_out'] or result['oom_killed']:
                    raise ValueError('Native test command exceeded resource limits')
            report['tests'] = tests = junit(control / REPORT, config['required_tests'])
        finally:
            shutil.rmtree(control)
        if tests['failed'] or set(config['required_tests']).intersection(tests['skipped']) or tests['missing']:
            report.update(verdict='fail', explanation='Native tests failed or required tests did not pass.')
        elif result['exit_code'] == 0:
            report.update(verdict='pass', explanation='All required native tests passed.')
        else:
            report['error_kind'] = 'nonzero_runner_exit'
    except Exception as exc:
        report.update(error_kind=type(exc).__name__, detail=str(exc))
    write_json(staged / 'execution.json', report)
    return report
