"""Independent assessments of saved trees; no coding-model reruns or transcripts."""
from pathlib import Path
import re
import shutil
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

POLICY = 'behavior-preserving-private-checks-v3-source-assessment'
PROMPT = """You are an independent evaluator of a saved candidate, not its coding agent.
Start with /mineval-input/baseline-results.json and task.json. Inspect only failing
checks and the candidate code they exercise; consult verification/extraction-manifest.json
for original evidence. Decide promptly whether behavior is wrong or only the private
check interface differs. For incorrect behavior, report no adaptations. Do not search
for unrelated improvements, rerun successful checks, install tools or fix production.
A missing implementation is not a legitimate alternative: never create or simulate
production behavior, add passing stubs, or hide missing implementation. For a legitimate
alternative, propose the smallest interface change preserving every behavioral assertion. Stop when evidence supports that decision.
Write only /mineval-control/assessment.json with exactly:
{adaptations: [{path, content, reason, evidence: [{commit, path}]}],
 limitations: [string], inconclusive_reason: string|null, error_reason?: string|null,
 behavior_failure_reason?: string|null, candidate_evidence?: [{path, reason}]}.
Adapt only selected private test interfaces when a legitimate implementation needs it.
Keep assertions and behavioral obligations; never weaken checks to pass wrong code,
require historical code shape, or invent obligations. Cite original extracted evidence
for every adaptation. A source-supported behavior failure requires behavior_failure_reason
and nonempty candidate_evidence citing existing candidate files with specific reasons.
For an inconclusive, execution-error, or adaptation decision, omit candidate_evidence
or set it to an empty array; include it only with behavior_failure_reason.
Inspect source and rule out legitimate equivalent interfaces before making that finding;
missing modules alone are insufficient. Do not claim assertions ran when collection failed.
A behavior failure cannot be combined with adaptations, error_reason or inconclusive_reason.
If setup, infrastructure or execution broke, provide error_reason. If evidence is insufficient/flaky/not defensibly adaptable,
explain inconclusive_reason. Do not edit production code. This report proposes checks;
Mineval executes them in a separate fresh sandbox. Do not save transcripts.
"""


def validate_assessment(value, artifacts, candidate=None):
    if (not isinstance(value, dict) or not {'adaptations', 'limitations', 'inconclusive_reason'} <= set(value)
            or set(value) - {'adaptations', 'limitations', 'inconclusive_reason', 'error_reason',
                             'behavior_failure_reason', 'candidate_evidence'}
            or not strings(value['limitations']) or not isinstance(value['adaptations'], list)
            or (value['inconclusive_reason'] is not None and
                (not isinstance(value['inconclusive_reason'], str) or not value['inconclusive_reason'].strip()))):
        raise ValueError('Invalid evaluator assessment')
    if value.get('error_reason') is not None and (not isinstance(value['error_reason'], str)
            or not value['error_reason'].strip()):
        raise ValueError('Evaluator execution error requires an explanation')
    behavior = value.get('behavior_failure_reason')
    evidence = value.get('candidate_evidence', [])
    if not isinstance(evidence, list):
        raise ValueError('Invalid candidate source evidence')
    if behavior is not None:
        if (not isinstance(behavior, str) or not behavior.strip() or value['adaptations'] or
                value['inconclusive_reason'] is not None or value.get('error_reason') is not None or
                not isinstance(evidence, list) or not evidence or candidate is None):
            raise ValueError('Source-supported failure requires exclusive reason and candidate evidence')
        files = inventory(candidate)
        seen_evidence = set()
        for item in evidence:
            if (not isinstance(item, dict) or set(item) != {'path', 'reason'} or
                    not isinstance(item['path'], str) or not isinstance(item['reason'], str) or
                    not item['reason'].strip()):
                raise ValueError('Invalid candidate source evidence')
            path = str(relative(item['path']))
            if path not in files or path in seen_evidence:
                raise ValueError('Candidate source evidence must be an existing unique file')
            seen_evidence.add(path)
    elif evidence:
        raise ValueError('Candidate evidence requires a source-supported failure')
    known = {(a['commit'], a['path']) for a in artifacts}
    paths = {a['path'] for a in artifacts}
    seen = set()
    for item in value['adaptations']:
        if (not isinstance(item, dict) or set(item) != {'path', 'content', 'reason', 'evidence'}
                or item['path'] not in paths or item['path'] in seen
                or not isinstance(item['content'], str) or not item['content'].strip()
                or not isinstance(item['reason'], str) or not item['reason'].strip()
                or not isinstance(item['evidence'], list) or not item['evidence']):
            raise ValueError('Adaptation requires a selected test, content, reason and evidence')
        seen.add(item['path'])
        for ref in item['evidence']:
            if (not isinstance(ref, dict) or set(ref) != {'commit', 'path'}
                    or (ref['commit'], ref['path']) not in known):
                raise ValueError('Adaptation must cite original extracted evidence')
    return value


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


def execute_checks(source_tree, destination, staged, verification, artifacts, adaptations, config, image, runner):
    report = {'commands': [], 'verdict': 'error', 'explanation': 'Verification infrastructure failed.'}
    staged.mkdir()
    try:
        workspace = destination
        copy_tree(source_tree, workspace)
        adapted = {a['path']: a for a in adaptations}
        original_inventory = inventory(verification)
        selected = []
        for item in artifacts:
            path = str(relative(item['path']))
            target = workspace / path
            # Candidate may replace a test directory with a regular file (or vice versa).
            current = workspace
            for part in relative(path).parts[:-1]:
                current = current / part
                if current.is_file():
                    current.unlink()
                current.mkdir(exist_ok=True)
            if target.is_dir():
                shutil.rmtree(target)
            source = verification / relative(item['stored_path'])
            if path in adapted:
                target.write_text(adapted[path]['content'])
            else:
                shutil.copyfile(source, target)
            target.chmod(0o755 if original_inventory[item['stored_path']]['executable'] else 0o644)
            selected.append(item | {'executed_sha256': digest(target), 'adapted': path in adapted})
        write_json(staged / 'selected-checks.json', selected)
        limits = Limits(**config.get('limits', {}))
        spec = SandboxSpec(image, ('/bin/sh', '-c', 'while :; do sleep 1; done'), limits,
            workspace=workspace, network=config.get('network', 'bridge'),
            workdir=config.get('workdir', '/workspace'), readonly_paths=tuple(a['path'] for a in artifacts))
        with runner.container(spec) as container:
            container.start()
            report.update(image_id=container.image_id, container_id=container.id)
            cache_stats = getattr(container, 'cache_stats', None)
            if isinstance(cache_stats, dict):
                report['download_cache'] = cache_stats
            deadline = time.monotonic() + limits.timeout_seconds
            for step in config['commands']:
                budget = min(step.get('timeout_seconds', limits.timeout_seconds), deadline - time.monotonic())
                if budget <= 0:
                    raise TimeoutError('Evaluation deadline exhausted')
                result = container.exec(step['argv'], timeout=budget, workdir=spec.workdir)
                code = result['exit_code']
                status = ('error' if result['timed_out'] or result['oom_killed'] else
                          'passed' if code == 0 else
                          'failed' if step['kind'] == 'tests' and code in step.get('failure_exit_codes', [1]) else
                          'inconclusive' if step['kind'] == 'tests' and code in step.get('inconclusive_exit_codes', [2]) else 'error')
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


def assess_checks(candidate, verification, artifacts, config, image, runner, baseline,
                  temp, task_summary, *, command=None, adapter=None, auth_file=None,
                  evaluator_timeout=180, proposed_path=None, provenance_path=None):
    """Run the shared isolated evaluator; validate citations before any rerun."""
    from .agents import CodexAdapter, provision_auth
    if adapter and (command or not isinstance(adapter, CodexAdapter)):
        raise ValueError('Use explicit evaluator argv or the dedicated Codex adapter')
    if auth_file and not isinstance(adapter, CodexAdapter):
        raise ValueError('Only Codex may receive authentication')
    copy_tree(candidate, temp / 'agent')
    control = temp / 'control'; control.mkdir()
    inputs = temp / 'inputs'; inputs.mkdir()
    copy_tree(verification, inputs / 'verification')
    write_json(inputs / 'task.json', {'task_summary': task_summary})
    write_json(inputs / 'config.json', config)
    write_json(inputs / 'baseline-results.json', baseline)
    (inputs / 'prompt.txt').write_text(PROMPT)
    if adapter:
        command = adapter.stage(inputs, required_files=('assessment.json',))
    if auth_file:
        provision_auth(auth_file, inputs / 'auth.json')
    agent_limits = config.get('limits', {}) | {'timeout_seconds': evaluator_timeout}
    spec = SandboxSpec(image, tuple(command), Limits(**agent_limits),
        workspace=temp / 'agent', control=control, inputs=inputs, network='bridge', retain_logs=False)
    evaluator = {'session_id': uuid4().hex, 'harness': adapter.name if adapter else 'explicit-command',
                 'model': adapter.model if adapter else 'unspecified',
                 'instructions_sha256': digest(inputs / 'prompt.txt')}
    if adapter and getattr(adapter, 'effort', None):
        evaluator['effort'] = adapter.effort
    if provenance_path is not None:
        write_json(provenance_path, evaluator)
    with runner.container(spec) as container:
        evaluator['image_id'] = container.image_id
        if provenance_path is not None:
            write_json(provenance_path, evaluator)
        container.start()
        timed_out = container.wait(spec.limits.timeout_seconds)
        state = container.inspect()
        if timed_out or state.get('OOMKilled') or state['Running'] or state['ExitCode']:
            from .sandbox import log_agent_failure
            log_agent_failure('evaluator.failed', container, state, timed_out)
            if timed_out:
                raise TimeoutError('Evaluator timed out')
            raise ValueError('Evaluator did not complete')
    proposal = read_json(control / 'assessment.json')
    if proposed_path is not None:
        write_json(proposed_path, proposal)
    assessment = validate_assessment(proposal, artifacts, candidate)
    return assessment, evaluator


def evaluate(store, task_id, run_id, config_path, *, evaluation_id=None, sandbox=None,
             command=None, adapter=None, auth_file=None, evaluator_timeout=180):
    """Config is an explicit trusted operator selection; evaluator proposes adaptations."""
    from .agents import CodexAdapter, provision_auth
    if adapter and (command or not isinstance(adapter, CodexAdapter)):
        raise ValueError('Use explicit evaluator argv or the dedicated Codex adapter')
    if auth_file and not isinstance(adapter, CodexAdapter):
        raise ValueError('Only Codex may receive authentication')
    runner = sandbox or DockerSandbox()
    from .downloads import cache_task
    cache_task(runner, store, task_id)
    evaluation_id = identifier(evaluation_id or uuid4().hex)
    config = load_config(config_path, evaluation=True)
    if not any(c['kind'] == 'tests' for c in config['commands']):
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
                baseline = execute_checks(temp / 'candidate', temp / 'baseline-workspace', staged / 'baseline',
                    verification, artifacts, [], config, image, runner)
                report.update(baseline)
                assessment = {'adaptations': [], 'limitations': [], 'inconclusive_reason': None}
                if (command or adapter) and baseline['verdict'] in ('fail', 'inconclusive'):
                    assessment, report['evaluator'] = assess_checks(
                        temp / 'candidate', verification, artifacts, config, image, runner, baseline,
                        temp, run['task_instructions'], command=command, adapter=adapter,
                        auth_file=auth_file, evaluator_timeout=evaluator_timeout,
                        proposed_path=staged / 'assessment-proposal.json',
                        provenance_path=staged / 'evaluator.json')
                report['adaptations'] = assessment['adaptations']
                report['limitations'].extend(assessment['limitations'])
                write_json(staged / 'assessment.json', assessment)
                # Only declared behavioral/inconclusive outcomes may be adapted.
                # Unknown exit codes and infrastructure errors never invoke AI.
                can_adapt = baseline['verdict'] in ('fail', 'inconclusive')
                if assessment.get('behavior_failure_reason'):
                    report.update(verdict='fail', explanation=assessment['behavior_failure_reason'],
                                  failure_basis='source_assessment', candidate_evidence=assessment['candidate_evidence'])
                elif assessment.get('error_reason'):
                    report.update(verdict='error', explanation=assessment['error_reason'])
                elif assessment['inconclusive_reason'] and baseline['verdict'] != 'error':
                    report.update(verdict='inconclusive', explanation=assessment['inconclusive_reason'])
                elif assessment['adaptations'] and can_adapt:
                    report.pop('error_kind', None)
                    report.update(execute_checks(temp / 'candidate', temp / 'adapted-workspace',
                        staged / 'adapted', verification, artifacts, assessment['adaptations'], config, image, runner))
                else:
                    report.update(baseline)
        except KeyboardInterrupt:
            report.update(verdict='error', explanation='Evaluation cancelled; no task failure inferred.', error_kind='interrupted')
        except Exception as exc:
            # Provenance is written before startup and updated once the image is known.
            provenance = staged / 'evaluator.json'
            if provenance.exists():
                report['evaluator'] = read_json(provenance)
            # No raw infrastructure exceptions/session output in durable records.
            report.update(verdict='error', explanation='Verification or evaluation infrastructure failed.',
                          error_kind=type(exc).__name__)
        report['limitations'].append('Read-only checks prevent file replacement, not adversarial runtime manipulation. '
            'Command selection and behavioral equivalence of adaptations require inspection; provenance is validated, semantics are not mechanically proved.')
        write_json(staged / 'report.json', report)
        target = task / 'evaluation-evidence' / evaluation_id
        seal(staged, target)
        result = store._reserve_record(task_id, 'evaluations', evaluation_id, {
            'schema_version': 2, 'task_id': task_id, 'evaluation_id': evaluation_id,
            'evaluation_version': version, 'run_id': run_id, 'prepared_revision': revision,
            'run_sha256': digest(task / 'runs' / (run_id + '.json')),
            'output_sha256': digest(output_path(store, task_id, run_id) / 'artifact-hashes.json'),
            'verification_sha256': verification_hash, 'evidence_sha256': checked(target),
            'policy': POLICY, 'config_sha256': digest(target / 'config.json'),
            'finished_at': now(), 'status': 'completed', **report})

    from .memory import record_evaluation
    record_evaluation(store, task_id, evaluation_id)
    return result


def compare(store, definition):
    """Explicit assessments only; never silently choose the most favorable reevaluation."""
    from .benchmark import compare as compare_runs
    if not isinstance(definition, dict) or set(definition) != {'left', 'right'}:
        raise ValueError('Require left and right evaluation selections')
    evaluations, runs = {}, {}
    for side in ('left', 'right'):
        if not isinstance(definition[side], list) or not definition[side]:
            raise ValueError('Each side requires a nonempty evaluation list')
        evaluations[side], runs[side] = {}, []
        for ref in definition[side]:
            if not isinstance(ref, dict) or set(ref) != {'task_id', 'evaluation_id'}:
                raise ValueError('Select task_id and evaluation_id explicitly')
            record = store.inspect_evaluation(**ref)
            from .publication import published
            if not published(store, ref['task_id'], record['run_id'], record['evaluation_id']):
                raise ValueError('Required evaluation and review are not complete for this assessment')
            if record['schema_version'] != 2:
                raise ValueError('Comparison requires executed evaluations')
            key = (ref['task_id'], record['prepared_revision']['version'])
            if any(k[0] == key[0] for k in evaluations[side]):
                raise ValueError('Select one assessment per task per side')
            evaluations[side][key] = record
            runs[side].append({'task_id': ref['task_id'], 'run_id': record['run_id']})
    result = compare_runs(store, runs)
    common = [(r['task_id'], r['version']) for r in result['common_revisions']]
    for key in common:
        left, right = (evaluations[s][key] for s in ('left', 'right'))
        if any(left[k] != right[k] for k in ('policy', 'config_sha256', 'verification_sha256')):
            raise ValueError('Common assessments require the same evaluation policy, config and verification')
        if left.get('image_id') and right.get('image_id') and left['image_id'] != right['image_id']:
            raise ValueError('Common assessments require the same resolved verification image')
    result['task_verdicts_available'] = True
    result['assessments'] = {s: list(v.values()) for s, v in evaluations.items()}
    result['rates'] = {}
    for side in ('left', 'right'):
        counts = {v: sum(evaluations[side][k]['verdict'] == v for k in common)
                  for v in ('pass', 'fail', 'inconclusive', 'error')}
        result['rates'][side] = {'counts': counts, 'denominator': len(common),
            'pass_rate': counts['pass'] / len(common) if common else None,
            'decided_coverage': (counts['pass'] + counts['fail']) / len(common) if common else None}
    return result
