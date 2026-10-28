"""Sequential benchmark attempts; immutable identities and separately sealed results.

Only materialized historical workspaces cross the agent boundary. Results describe
harness execution, never whether the coding task was solved (evaluation is separate).
"""
from datetime import datetime, timezone
from pathlib import Path
import os
import re
import shutil
import signal
import tempfile
from contextlib import contextmanager
from uuid import uuid4

from .agents import CodexAdapter, FakeAdapter, POLICY, run_agent
from .artifacts import digest, inventory, read_json, unpack, write_json
from .preparation import identifier
from .sandbox import Limits


def now():
    return datetime.now(timezone.utc).isoformat()


def output_path(store, task_id, run_id):
    return store.task(task_id) / 'run-outputs' / identifier(run_id)


def inspect_attempt(store, task_id, run_id):
    """Caller holds task lock. Old reserved identities remain readable unchanged."""
    identity = store._inspect_record(task_id, 'runs', run_id)
    target = output_path(store, task_id, run_id)
    if not target.exists():
        return identity
    actual = inventory(target)
    actual.pop('artifact-hashes.json', None)
    if actual != read_json(target / 'artifact-hashes.json'):
        raise ValueError('Run output artifact hash mismatch')
    terminal = read_json(target / 'terminal.json')
    if terminal['identity_sha256'] != digest(store.task(task_id) / 'runs' / (run_id + '.json')):
        raise ValueError('Run output identity mismatch')
    return identity | terminal


def seal(store, task_id, run_id, staged, terminal):
    target = output_path(store, task_id, run_id)
    if target.exists():
        raise ValueError('Run already finished; create a new attempt')
    write_json(staged / 'terminal.json', terminal | {
        'finished_at': now(),
        'identity_sha256': digest(store.task(task_id) / 'runs' / (run_id + '.json'))})
    write_json(staged / 'artifact-hashes.json', inventory(staged))
    for path in staged.rglob('*'):
        if path.is_file():
            path.chmod(0o444)
    target.parent.mkdir(exist_ok=True)
    os.rename(staged, target)
    return inspect_attempt(store, task_id, run_id)


@contextmanager
def termination_handler():
    """CLI SIGTERM follows the same cleanup path as Ctrl-C; library owns no signals."""
    previous = signal.getsignal(signal.SIGTERM)
    def cancel(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, cancel)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run(store, task_id, version, *, run_id=None, adapter=None, context=None,
        auth_file=None, timeout=None, sandbox=None, suite=None):
    adapter = adapter or FakeAdapter()
    if not isinstance(adapter, (FakeAdapter, CodexAdapter)):
        raise ValueError('Only dedicated fake and Codex adapters are supported')
    if not isinstance(adapter.model, str) or not adapter.model.strip():
        raise ValueError('Supply a model identifier')
    if auth_file and not isinstance(adapter, CodexAdapter):
        raise ValueError('Only Codex may receive authentication')
    run_id = identifier(run_id or uuid4().hex)
    from .downloads import cache_task
    from .agents import DockerSandbox
    sandbox = cache_task(sandbox or DockerSandbox(context=context), store, task_id, version=version)
    with store.operation(task_id):
        revision = store._inspect_revision(task_id, version)
        frozen = store.task(task_id) / 'versions' / version
        config = read_json(frozen / 'verify-config.json')
        diagnostic_image = read_json(frozen / 'last-diagnostic.json').get('image_id', '')
        image = (diagnostic_image if re.fullmatch(r'sha256:[0-9a-f]{64}', diagnostic_image)
                 else config['image'])
        limits_config = config.get('limits', {}).copy()
        if timeout is not None:
            limits_config['timeout_seconds'] = timeout
        limits = Limits(**limits_config)
        fmt = read_json(frozen / 'workspace-format.json') if (frozen / 'workspace-format.json').exists() else {}
        if fmt.get('git_history', {}).get('format') != 'base-pack-v1':
            raise ValueError('Benchmark requires a revision with sanitized pre-base Git history')
        if suite is not None:
            # Pin and validate association rather than accepting caller-invented membership.
            suite_record = store.inspect_suite(suite)
            if revision not in suite_record['members']:
                raise ValueError('Revision is not a member of the selected suite')
            suite = {'suite_id': suite, 'sha256': digest(store.root / 'suites' / suite / 'suite.json')}
        instruction_source = (frozen / 'task-instructions.json' if (frozen / 'task-instructions.json').exists()
                              else store.task(task_id) / 'metadata.json')
        instructions = read_json(instruction_source)['task_summary']
        import hashlib
        prompt_hash = hashlib.sha256((POLICY + '\nTask:\n' + instructions).encode()).hexdigest()
        # Reserve once before launch. A killed host leaves this identity visible;
        # recover() appends an infrastructure error without rerunning the agent.
        store._reserve_record(task_id, 'runs', run_id, {
            'schema_version': 2, 'task_id': task_id, 'run_id': run_id,
            'prepared_revision': revision, 'suite': suite,
            'model': {'name': adapter.model, 'reasoning_effort': adapter.effort}, 'harness': {'name': adapter.name, 'adapter_version': 1},
            'status': 'running', 'started_at': now(), 'task_instructions': instructions,
            'instructions_sha256': prompt_hash,
            'operation': {'image': image, 'configured_image': config['image'],
                          'limits': limits.__dict__, 'network': 'bridge'},
            'task_verdict': None})
        with tempfile.TemporaryDirectory(prefix='.bench-', dir=store.root) as temp:
            temp = Path(temp)
            staged = temp / 'result'; staged.mkdir()
            terminal = {'status': 'error', 'failure_kind': 'infrastructure', 'output_available': False}
            try:
                # materialize verifies all frozen receipts, but exports only tree/history.
                store._materialize(task_id, version, temp / 'historical')
                result = run_agent(temp / 'historical', instructions, temp / 'agent-output',
                    image=image, adapter=adapter, context=context, limits=limits,
                    auth_file=auth_file, sandbox=sandbox, capture_interrupt=True)
                shutil.move(temp / 'agent-output', staged / 'agent')
                status = result['status']
                invalid_completion = status == 'error' and result['exit_code'] == 0
                terminal = {'status': ('error' if invalid_completion else
                                       {'error': 'crashed', 'oom': 'crashed'}.get(status, status)),
                            'failure_kind': ('invalid_completion' if invalid_completion else
                                             'agent_process' if status in ('error', 'oom') else None),
                            'output_available': True, 'agent_result': result}
            except KeyboardInterrupt:
                terminal = {'status': 'cancelled', 'failure_kind': 'interrupted_collection', 'output_available': False}
            except Exception:
                # Raw exceptions can contain subprocess output, credentials or user paths.
                # Preserve only a small operational classification; never session logs.
                terminal = {'status': 'error', 'failure_kind': 'infrastructure_or_output', 'output_available': False}
            return seal(store, task_id, run_id, staged, terminal)


def run_suite(store, suite_id, *, run_id=None, **options):
    suite = store.inspect_suite(suite_id)
    # Preflight every member before launching any agent; each attempt still gets a
    # separate durable identity. Cancellation stops the sequence, never auto-retries.
    for member in suite['members']:
        with store.operation(member['task_id']):
            if store._inspect_revision(member['task_id'], member['version']) != member:
                raise ValueError('Suite revision identity mismatch')
    prefix = identifier(run_id or uuid4().hex)
    if len(prefix) > 90:
        raise ValueError('Suite run ID prefix must be at most 90 characters')
    results = []
    for index, member in enumerate(suite['members'], 1):
        result = run(store, member['task_id'], member['version'],
                     run_id=f'{prefix}-{index:03d}', suite=suite_id, **options)
        results.append(result)
        if result['status'] == 'cancelled':
            break
    return results


def recover(store, task_id, run_id):
    """After an unclean host exit, explicitly seal an unfinished attempt as error.

    The task lock excludes an active runner. This never asserts task failure or
    reruns work, and cannot recover an uncollected workspace after SIGKILL.
    """
    with store.operation(task_id, allow_rejected=True):
        record = inspect_attempt(store, task_id, run_id)
        if record['status'] != 'running':
            raise ValueError('Only an unfinished benchmark attempt can be recovered')
        with tempfile.TemporaryDirectory(dir=store.root) as temp:
            staged = Path(temp) / 'result'; staged.mkdir()
            return seal(store, task_id, run_id, staged, {
                'status': 'error', 'failure_kind': 'unclean_host_exit', 'output_available': False})


def list_runs(store, task_id=None):
    task_ids = [identifier(task_id)] if task_id else [t['task_id'] for t in store.list_tasks()]
    result = []
    for task in task_ids:
        with store.operation(task, allow_rejected=True):
            for path in sorted((store.task(task) / 'runs').glob('*.json')):
                result.append(inspect_attempt(store, task, path.stem))
    return result


def export(store, task_id, run_id, destination):
    destination = Path(destination).resolve()
    if destination == store.root.resolve() or store.root.resolve() in destination.parents:
        raise ValueError('Run export must be outside the store')
    with store.operation(task_id, allow_rejected=True):
        record = inspect_attempt(store, task_id, run_id)
        if not record.get('output_available'):
            raise ValueError('Run has no saved workspace')
        unpack(output_path(store, task_id, run_id) / 'agent/workspace.tar.gz', destination)
    return {'workspace': str(destination), 'task_id': task_id, 'run_id': run_id}


def compare(store, definition):
    """Report exact common revisions/coverage. No grading or cross-subset aggregates."""
    if not isinstance(definition, dict) or set(definition) != {'left', 'right'}:
        raise ValueError('Comparison requires left and right lists of task_id/run_id pairs')
    selections = {}
    for side in ('left', 'right'):
        refs = definition[side]
        if not isinstance(refs, list) or not refs:
            raise ValueError('Each comparison side requires a nonempty run list')
        records, identity = {}, None
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {'task_id', 'run_id'}:
                raise ValueError('Run selection requires task_id and run_id')
            record = store.inspect_run(ref['task_id'], ref['run_id'])
            if record['schema_version'] != 2 or record['status'] in ('running', 'reserved'):
                raise ValueError('Comparison requires finished benchmark attempts')
            ranking_identity = (record['model']['name'], record['model'].get('reasoning_effort'),
                                record['harness'])
            if identity is not None and ranking_identity != identity:
                raise ValueError('Each side must select one model + harness')
            identity = ranking_identity
            revision = record['prepared_revision']
            key = (revision['task_id'], revision['version'], revision['artifact_manifest_sha256'])
            if any(k[0] == key[0] for k in records):
                raise ValueError('Select one attempt per task on each comparison side')
            records[key] = record
        selections[side] = records
    left, right = selections['left'], selections['right']
    common = sorted(left.keys() & right.keys())
    return {'common_revisions': [left[k]['prepared_revision'] for k in common],
            'coverage': {'left_total': len(left), 'right_total': len(right), 'common': len(common),
                         'left_fraction': len(common) / len(left), 'right_fraction': len(common) / len(right)},
            'left_only': [left[k]['prepared_revision'] for k in sorted(left.keys() - right.keys())],
            'right_only': [right[k]['prepared_revision'] for k in sorted(right.keys() - left.keys())],
            'selections': {side: list(records.values()) for side, records in selections.items()},
            'task_verdicts_available': False}
