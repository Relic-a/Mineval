"""Independent assessments of saved trees; no coding-model reruns or transcripts."""
from pathlib import Path
import hashlib
import json
import re
import tempfile
import time
from uuid import uuid4

from .devlog import event
from .artifacts import lock, copy_tree, digest, inventory, read_json, relative, unpack, write_json
from .benchmark import inspect_attempt, now, output_path
from .preparation import identifier
from .review import checked, seal, strings
from .sandbox import DockerSandbox, Limits, SandboxSpec
from .verification import load_config

POLICY = 'isolated-argv-v1'

def inspect(store, task_id, evaluation_id):
    """Caller holds task lock. Legacy reservations remain readable."""
    record = store._inspect_record(task_id, 'evaluations', evaluation_id)
    if record['schema_version'] == 1:
        return record
    evidence = store.task(task_id) / 'evaluation-evidence' / identifier(evaluation_id)
    if checked(evidence) != record['evidence_sha256']:
        raise ValueError('Evaluation evidence identity mismatch')
    run = inspect_attempt(store, task_id, record['run_id'])
    if (digest(store.task(task_id) / 'runs' / (record['run_id'] + '.json')) != record['run_sha256']
            or digest(output_path(store, task_id, record['run_id']) / 'artifact-hashes.json') != record['output_sha256']
            or run['prepared_revision'] != record['prepared_revision']
            or store._inspect_revision(task_id, record['prepared_revision']['version']) != record['prepared_revision']):
        raise ValueError('Evaluation input identity mismatch')
    return record


def execute_checks(source_tree, destination, staged, verification, artifacts, adaptations, config, image, runner, *, prepare_workspace=None):
    report = {'commands': [], 'verdict': 'error', 'explanation': 'Verification infrastructure failed.'}
    if adaptations:
        raise ValueError('Frozen checks cannot be adapted to a candidate')
    if config.get('grading_type') == 'native-script-v1':
        from .native_grading import execute
        return execute(source_tree, destination, staged, verification, artifacts, config, image, runner,
                       prepare_workspace=prepare_workspace)
    if config.get('grading_type') == 'isolated-http-v1':
        from .http_grading import execute
        return execute(source_tree, destination, staged, verification, artifacts, config, image, runner)
    staged.mkdir()
    if config.get('grading_type') != 'isolated-argv-v1':
        report.update(error_kind='unsupported_grading_type',
                      explanation='This grading type is unsupported for isolated grading; shared-environment tests cannot run safely.')
        write_json(staged / 'execution.json', report)
        return report
    try:
        workspace = destination
        copy_tree(source_tree, workspace)
        selected = []
        cases = []
        if sum(step['kind'] == 'tests' for step in config['commands']) != 1:
            raise ValueError('Isolated grading requires exactly one tests command')
        for item in artifacts:
            path = str(relative(item['path']))
            source = verification / relative(item['stored_path'])
            raw = source.read_bytes()
            if len(raw) > 1024 * 1024:
                raise ValueError('Isolated test file is too large')
            document = json.loads(raw)
            if not isinstance(document, dict) or set(document) != {'cases'} or not isinstance(document['cases'], list) or not document['cases']:
                raise ValueError('Expected a nonempty isolated test case list')
            for case in document['cases']:
                if (not isinstance(case, dict) or set(case) != {'input', 'expected'} or
                        any(not isinstance(case[k], str) or '\0' in case[k] or len(case[k].encode()) > 32768 for k in ('input', 'expected'))):
                    raise ValueError('Invalid isolated test case')
                cases.append(case)
            selected.append(item | {'executed_sha256': hashlib.sha256(raw).hexdigest(), 'adapted': False})
        if not cases or len(cases) > 1000:
            raise ValueError('Isolated grading requires between 1 and 1000 cases')
        write_json(staged / 'selected-checks.json', selected)
        limits = Limits(**config.get('limits', {}))
        spec = SandboxSpec(image, ('/bin/sh', '-c', 'while :; do sleep 1; done'), limits,
            workspace=workspace, network=config.get('network', 'bridge'),
            workdir=config.get('workdir', '/workspace'), services=config.get('services', ()))
        with runner.container(spec) as container:
            container.start()
            report.update(image_id=container.image_id, container_id=container.id)
            service_images = getattr(container, 'service_images', None)
            if isinstance(service_images, dict):
                report['service_images'] = service_images
            cache_stats = getattr(container, 'cache_stats', None)
            if isinstance(cache_stats, dict):
                report['download_cache'] = cache_stats
            deadline = time.monotonic() + limits.timeout_seconds
            for step in config['commands']:
                for case in cases if step['kind'] == 'tests' else [None]:
                    budget = min(step.get('timeout_seconds', limits.timeout_seconds), deadline - time.monotonic())
                    if budget <= 0:
                        raise TimeoutError('Evaluation deadline exhausted')
                    result = container.exec(step['argv'] + ([case['input']] if case is not None else []), timeout=budget, workdir=spec.workdir)
                    code = result['exit_code']
                    status = ('error' if result['timed_out'] or result['oom_killed'] else
                              'passed' if code == 0 else
                              'failed' if step['kind'] == 'tests' and code in step.get('failure_exit_codes', [1]) else
                              'inconclusive' if step['kind'] == 'tests' and code in step.get('inconclusive_exit_codes', [2]) else 'error')
                    if case is not None and status == 'passed':
                        status = 'error' if result['logs_truncated'] else 'passed' if result['stdout'] == case['expected'] else 'failed'
                    reason = ('timeout' if result['timed_out'] else 'oom' if result['oom_killed'] else
                              'exit_code')
                    event('evaluation.command.classified', container_id=container.id, command_index=len(report['commands']),
                          kind=step['kind'], status=status, reason=reason, exit_code=code,
                          timeout_seconds=budget, timed_out=result['timed_out'], oom_killed=result['oom_killed'],
                          resources=result.get('resources', {}), pressure_stall_us=result.get('pressure_stall_us'), limits=vars(limits),
                          started_at_monotonic=result.get('started_at_monotonic'),
                          ended_at_monotonic=result.get('ended_at_monotonic'))
                    report['commands'].append({'name': step['name'], 'kind': step['kind'], 'status': status, **result})
                    if status == 'error' or not container.inspect()['Running']:
                        raise ValueError('Verification execution failed')
        failed = any(c['status'] == 'failed' for c in report['commands'])
        uncertain = any(c['status'] == 'inconclusive' for c in report['commands'])
        report.update(verdict='inconclusive' if uncertain else 'fail' if failed else 'pass',
            explanation='Private checks could not establish a defensible verdict.' if uncertain else
                        'Private behavioral checks failed.' if failed else 'All selected private checks passed.')
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        report.update(verdict='error', explanation='Verification execution or infrastructure failed.',
                      error_kind=type(exc).__name__)
    write_json(staged / 'execution.json', report)
    return report


def evaluate(store, task_id, run_id, config_path, *, evaluation_id=None, sandbox=None,
             command=None, adapter=None, auth_file=None, evaluator_timeout=180):
    """Run fixed private checks against a saved candidate."""
    if command or adapter or auth_file:
        raise ValueError('AI-assisted evaluation is no longer supported')
    runner = sandbox or DockerSandbox()
    from .downloads import cache_task
    cache_task(runner, store, task_id)
    evaluation_id = identifier(evaluation_id or uuid4().hex)
    config = load_config(config_path, evaluation=True)
    if config.get('grading_type') != 'native-script-v1' and not any(c['kind'] == 'tests' for c in config['commands']):
        raise ValueError('Evaluation requires at least one private tests command')
    if any(c.get('expected_failure') for c in config['commands']):
        raise ValueError('Evaluation cannot exempt expected failures')
    with (lock(store.root / 'locks/evaluations' / identifier(task_id) / (identifier(run_id) + '.lock')),
          lock(store.root / 'locks/evaluation-records' / identifier(task_id) / (evaluation_id + '.lock')),
          store.operation(task_id, allow_rejected=True, shared=True),
          tempfile.TemporaryDirectory(dir=store.root) as temp):
        temp = Path(temp)
        task = store.task(task_id)
        record_path = task / 'evaluations' / (evaluation_id + '.json')
        if record_path.exists() or record_path.with_suffix('.sha256').exists() or (task / 'evaluation-evidence' / evaluation_id).exists():
            raise ValueError('Evaluation ID already exists; use a new ID')
        run = inspect_attempt(store, task_id, run_id)
        if run['schema_version'] != 2 or run['status'] in ('running', 'reserved'):
            raise ValueError('Evaluation requires a finished benchmark attempt')
        revision = store._inspect_revision(task_id, run['prepared_revision']['version'])
        cache_task(runner, store, task_id, version=revision['version'])
        if revision != run['prepared_revision']:
            raise ValueError('Run revision identity mismatch')
        frozen = task / 'versions' / revision['version']
        frozen_config_path = frozen / 'evaluation-config.json'
        if frozen_config_path.exists() and config != read_json(frozen_config_path):
            raise ValueError('Evaluation configuration differs from the frozen reviewed checks')
        verification = frozen / 'private/verification'
        verification_hash = checked(verification)
        manifest = read_json(verification / 'extraction-manifest.json')
        artifacts = manifest['artifacts']
        paths = [str(relative(a['path'])) for a in artifacts]
        if len(set(paths)) != len(paths):
            raise ValueError('Evaluation needs one authoritative source per destination path')
        # Other runs may be between atomic record and receipt publication. Only
        # this run's records affect its version, and its run lock excludes writers.
        previous = []
        for p in (task / 'evaluations').glob('*.json'):
            if not p.with_suffix('.sha256').exists():
                continue  # Interrupted publication; preserve it without treating it as evidence.
            record = store._inspect_record(task_id, 'evaluations', p.stem)
            if record['run_id'] == run_id:
                previous.append(record)
        version = 1 + max((r.get('evaluation_version', 0) for r in previous if r['run_id'] == run_id), default=0)
        staged = temp / 'evidence'; staged.mkdir()
        write_json(staged / 'config.json', config)
        copy_tree(verification, staged / 'verification')
        report = {'commands': [], 'adaptations': [], 'limitations': list(manifest.get('plan', {}).get('limitations', [])),
                  'verdict': 'error', 'explanation': 'Evaluation infrastructure failed.', 'evaluator': None}
        image = config['image']
        pinned = read_json(frozen / 'last-diagnostic.json').get('image_id', '')
        frozen_config = read_json(frozen / 'verify-config.json')
        if image == frozen_config['image'] and re.fullmatch(r'sha256:[0-9a-f]{64}', pinned):
            image = pinned
        try:
            if (run.get('failure_kind') == 'resource_exhaustion' or
                    (run.get('failure_kind') == 'resource_contention' and
                     run.get('agent_result', {}).get('timed_out'))):
                report.update(verdict='error', error_kind=('timeout' if run.get('agent_result', {}).get('timed_out')
                                                          and run['failure_kind'] == 'resource_contention'
                                                          else run['failure_kind']),
                    explanation='Run encountered resource exhaustion or a timeout; retry with sufficient capacity.')
            elif not run.get('output_available'):
                report.update(verdict='error', error_kind=run.get('failure_kind') or 'missing_output',
                    explanation='Run has no saved candidate workspace; execution infrastructure failed.')
            else:
                archive = output_path(store, task_id, run_id) / 'agent/workspace.tar.gz'
                unpack(archive, temp / 'candidate')
                def attach_history(workspace):
                    from .history import restore
                    fmt = read_json(frozen / 'workspace-format.json')['git_history']
                    restore(frozen / fmt['path'], fmt['base_commit'], workspace)
                    if (frozen / 'submodules.json').exists():
                        from .repository import attach_modules
                        attach_modules(workspace, frozen, read_json(frozen / 'submodules.json'))

                baseline = execute_checks(temp / 'candidate', temp / 'baseline-workspace', staged / 'baseline',
                    verification, artifacts, [], config, image, runner,
                    prepare_workspace=attach_history)
                report.update(baseline)

        except KeyboardInterrupt:
            report.update(verdict='error', explanation='Evaluation cancelled; no task failure inferred.', error_kind='interrupted')
        except Exception as exc:
            # No raw infrastructure exceptions/session output in durable records.
            report.update(verdict='error', explanation='Verification or evaluation infrastructure failed.',
                          error_kind=type(exc).__name__)
        write_json(staged / 'report.json', report)
        target = task / 'evaluation-evidence' / evaluation_id
        seal(staged, target)
        result = store._reserve_record(task_id, 'evaluations', evaluation_id, {
            'schema_version': 2, 'task_id': task_id, 'evaluation_id': evaluation_id,
            'evaluation_version': version, 'run_id': run_id, 'prepared_revision': revision,
            'run_sha256': digest(task / 'runs' / (run_id + '.json')),
            'output_sha256': digest(output_path(store, task_id, run_id) / 'artifact-hashes.json'),
            'verification_sha256': verification_hash, 'evidence_sha256': checked(target),
            'policy': config.get('grading_type') or POLICY, 'config_sha256': digest(target / 'config.json'),
            'grading_conditions': {'limits': vars(Limits(**config.get('limits', {}))),
                                   'network': config.get('network', 'bridge')},
            'project_image_id': report.get('image_id') or (image if re.fullmatch(r'sha256:[0-9a-f]{64}', image) else None),
            'finished_at': now(), 'status': 'completed', **report})

    from .memory import record_evaluation
    record_evaluation(store, task_id, evaluation_id)
    return result


def rates(records):
    counts = {v: sum(record['verdict'] == v for record in records)
              for v in ('pass', 'fail', 'inconclusive', 'error')}
    decided = counts['pass'] + counts['fail']
    n = len(records)
    return {'counts': counts, 'denominator': n, 'attempt_count': n,
            'infrastructure_errors': counts['error'],
            'pass_rate': counts['pass'] / decided if decided else None,
            'decided_coverage': decided / n if n else None}


def compare(store, definition):
    """Compare explicit assessments, with one vote per distinct saved attempt."""
    from .benchmark import compare as compare_runs, comparison_groups, condition_key, conditions
    from .memory import encoded
    if not isinstance(definition, dict) or set(definition) != {'left', 'right'}:
        raise ValueError('Require left and right evaluation selections')
    evaluations, refs, excluded, attempts = {}, {}, [], {}
    for side in ('left', 'right'):
        if not isinstance(definition[side], list) or not definition[side]:
            raise ValueError('Each side requires a nonempty evaluation list')
        evaluations[side], refs[side] = [], []
        seen = set()
        for ref in definition[side]:
            if not isinstance(ref, dict) or set(ref) != {'task_id', 'evaluation_id'}:
                raise ValueError('Select task_id and evaluation_id explicitly')
            record = store.inspect_evaluation(**ref)
            from .publication import published
            if not published(store, ref['task_id'], record['run_id'], record['evaluation_id']):
                raise ValueError('Required evaluation and review are not complete for this assessment')
            if record['schema_version'] != 2:
                raise ValueError('Comparison requires executed evaluations')
            attempt = (ref['task_id'], record['run_id'])
            if attempt in seen:
                excluded.append({'side': side, **ref, 'run_id': record['run_id'],
                                 'reason': 'attempt already selected'})
                continue
            seen.add(attempt)
            attempts[attempt] = store.inspect_run(*attempt)
            evaluations[side].append(record)
            refs[side].append({'task_id': ref['task_id'], 'run_id': record['run_id']})
    result = compare_runs(store, refs)

    def key(record):
        return condition_key(record, attempts[(record['task_id'], record['run_id'])])

    groups, common, mismatches = comparison_groups(evaluations, key)
    result['excluded'] = excluded + mismatches
    result['common_revisions'] = list({encoded(groups['left'][k][0]['prepared_revision']):
        groups['left'][k][0]['prepared_revision'] for k in common}.values())
    for side in ('left', 'right'):
        total = len(groups[side])
        other = 'right' if side == 'left' else 'left'
        result[side + '_only'] = [groups[side][k][0]['prepared_revision']
                                  for k in sorted(groups[side].keys() - groups[other].keys())]
        result['coverage'][side + '_total'] = total
        result['coverage'][side + '_fraction'] = len(common) / total
    result['coverage']['common'] = len(common)
    result['task_verdicts_available'] = True
    result['assessments'] = evaluations
    cohorts = {}
    for k in common:
        record = groups['left'][k][0]
        operation = conditions(attempts[(record['task_id'], record['run_id'])])
        # Different tasks may require different project images. Match the image
        # per task, then aggregate only identical coding budgets.
        budget = {field: value for field, value in operation.items() if field in ('limits', 'harness', 'network')}
        budget['grading'] = record.get('grading_conditions')
        entry = cohorts.setdefault(encoded(budget), {'conditions': budget, 'left': [], 'right': []})
        for side in ('left', 'right'):
            entry[side].extend(groups[side][k])
    result['cohorts'] = [{'conditions': cohort['conditions'], 'rates': {
        side: rates(cohort[side]) for side in ('left', 'right')}} for cohort in cohorts.values()]
    result['rates'] = (result['cohorts'][0]['rates'] if len(cohorts) == 1 else
                       {side: rates([]) for side in ('left', 'right')})
    return result
