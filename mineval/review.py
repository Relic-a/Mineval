"""Private producing/review sessions and exact-input approval receipts."""
from pathlib import Path
import tempfile
from uuid import uuid4

from .artifacts import copy_tree, digest, inventory, read_json, relative, write_json
from .preparation import identifier
from .sandbox import DockerSandbox, Limits, SandboxSpec

VALIDATION_POLICY = "private-evaluation-assessment-v3"

EXTRACTION = """You are the separate verification extractor. Inspect task.json, base,
cleaned, future and diffs. Do not edit the task. Write /mineval-control/selection.json:
{strategy: focused|fuller|unavailable, rationale: nonempty string, dependencies:
[string], instructions: nonempty string, limitations: [string], artifacts:
[{commit, path, purpose, test_only: true}], evaluation_config: {image, commands}}.
The evaluation_config object belongs inside selection.json, not only in the completion
message. Select meaningful behavioral tests,
fixtures and essential test-only dependencies from the supplied history. Never
select production implementation as test support. Explain behavioral obligations,
not arbitrary implementation choices. If no credible checks exist, use unavailable
and an empty artifacts list. Do not invent superficial checks. No transcripts.
Read /workspace/evidence-index.json first. Examine its exact future tree roots and
files, selected artifacts, configs and revision paths before claiming evidence is absent.
Future trees are under /workspace/future/<commit>/, not directly under
/workspace/future/. Inspect the selected commit's actual paths before concluding an
artifact is absent; prior selected original bytes are also in revision/extraction.
Also supply evaluation_config when runnable verification is available: a JSON object
with image (use the supplied runtime), network, optional limits, workdir, and commands
[{name, kind: setup|build|tests|smoke, argv: [strings]}]. Include a tests command
executing the selected private checks at /workspace paths. The extraction session's
/workspace/cleaned and /workspace/future trees are inspection copies only. During
validation and evaluation, the candidate root is /workspace, and each selected
artifact is overlaid at /workspace/<its selected path> before commands run. For
example, a selected tests/regression.mjs is executed as node tests/regression.mjs
with workdir /workspace; do not refer to /workspace/cleaned, /workspace/future,
or /workspace/extraction in evaluation_config. A generic repository test command
is insufficient unless it actually executes the selected checks. Include dependency
setup commands required by the supplied runtime. Supply the config whenever a
credible runnable check exists, and explain specifically if it cannot be run. Choose
checks that demonstrate the task's missing behavior on the cleaned base and succeed
on the relevant future tree when feasible; report nondiscriminating checks and failed
or inconclusive runs honestly. If the feature is absent on the base and its historical
test interface cannot collect, inspect the base source and diff for an equivalent
legitimate interface. Preserve the original tests and config, record that base assertions
did not run and why, and verify the future checks where feasible. Do not invent missing
production behavior or stubs to force base assertions to run. Verify intended cases
actually ran to completion wherever execution is feasible:
a zero exit alone can mask skipped cases or an early process exit. Classify unexpected
setup, harness, or premature termination separately from behavioral assertions. Do not
leave a test command running after its cases finish. For Node's test runner, investigate
lingering handles and, if supported and appropriate for the selected tests, consider
--test-force-exit; confirm all intended cases finished and runnable commands exit normally.
Do not assert historical implementation names or code shape where another implementation can
satisfy the behavioral obligation. Historical tests may nevertheless be useful when
their behavioral assertions are sound but their imports or API seams are specific to
the historical implementation. Identify those seams and limitations explicitly:
the independent evaluator can propose minimal test-interface adaptations for a
legitimate alternative, preserving behavioral assertions and original evidence.
Do not treat an import or collection failure as a proven behavioral failure. Select
such tests only if their obligations remain defensible and the reviewer can assess
the base/future evidence; otherwise report unavailable.
Distinguish behavioral
failure (exit 1) from inconclusive (2) and infrastructure errors (other codes).
Never exempt expected failures. This config is private, reviewed and frozen.
If no defensible runnable command exists, use unavailable and explain limitations.
When /workspace/revision exists, inspect its prior review, extraction, and validation
results. Address the reviewer’s reasons and empirically diagnose failing or incomplete
commands. Adapt the evaluation command or test harness when needed, without altering
selected historical artifact bytes. Verify cases actually execute and finish on both
base and future where feasible; report remaining coverage limitations honestly.
"""
REVIEW = """You are the independent third reviewer, after cleanup and extraction.
Read /workspace/evidence-index.json first and examine its exact paths and configs
before claiming evidence is absent. The selected evaluation config is in the extraction
manifest at plan.evaluation_config; runtime.json is the cleanup diagnostic config.
Inspect task.json, base, cleaned, future/diffs, cleanup.json, diagnostic.json,
extraction and any validation results. Check task integrity, every cleanup edit,
future/credential leakage, test-only provenance, meaningful behavioral relevance,
arbitrary assumptions, alternate legitimate solutions, dependencies and reproducibility.
Consider the independent evaluator's ability to adapt test interfaces, while requiring
that behavior assertions and their provenance remain intact. A missing historical
module alone is not proof of a behavioral failure.
Review the actual evaluation_config and private base/future validation results against
selected checks and exit-code semantics. Inspect baseline, full assessment, selected-check
hashes and any adapted rerun in each validation directory. Assess whether adaptations
only adjust an existing legitimate interface, preserve all assertions and do not invent
implementation or stubs; reject unsupported adaptations. Diagnostics alone do not validate private
checks. Require a runnable, reproducible private tests command that actually runs the
selected artifacts in the evaluation workspace, or explain why execution is impossible.
Distinguish an empirical assertion failure from collection or interface failure and
from an independent source-based finding that the behavior is absent. A missing module
alone does not establish absence: inspect the base source and diff, and the independent
assessment for an existing legitimate equivalent interface. If base execution is
demonstrably infeasible because the feature is absent and no legitimate interface can
be adapted, you may assess credible future-executed checks with the base limitation
clearly recorded; never claim base assertions ran or require production code or stubs
to force them to run. Independently assess original assertions and provenance,
legitimate alternatives and evaluator adaptability, reproducible future execution,
and coverage gaps. Passing on both trees is nondiscriminating; broken future checks or
future environment failures remain blockers. Do not approve misleading or unjustified
commands or treat uncovered obligations as tested. No outcome is automatic approval.
Write /mineval-control/review.json: {verdict: approve|revise|reject, reasons: [nonempty
strings], objective_preserved: boolean, snapshot_reviewed: boolean, changes:
[{path, reason}]}. Account for exactly each cleanup change. Approve only credible
verification and preserved tasks; explain unvalidated checks. You may inspect future
solutions here; never copy them into cleaned. No transcripts.
"""


def checked(directory):
    actual = inventory(directory)
    actual.pop('artifact-hashes.json', None)
    if actual != read_json(directory / 'artifact-hashes.json'):
        raise ValueError('Private preparation artifact hash mismatch')
    return digest(directory / 'artifact-hashes.json')


def seal(staged, target):
    write_json(staged / 'artifact-hashes.json', inventory(staged))
    target.parent.mkdir(parents=True, exist_ok=True)
    staged.rename(target)
    for path in target.rglob('*'):
        if path.is_file():
            path.chmod(0o444)


def inputs(store, task_id, extraction_id):
    task, draft = store.task(task_id), store.draft(task_id)
    cleanup = read_json(task / 'cleanup-latest.json')
    attempt = task / 'cleanups' / identifier(cleanup['cleanup_id'])
    cleanup_hash = checked(attempt)
    from .verification import load_config
    if (cleanup != read_json(attempt / 'cleanup-report.json') or
            cleanup['disposition'] != 'ready-for-review' or
            inventory(draft / 'workspace') != read_json(attempt / 'tree-hashes.json') or
            load_config(draft / 'control/verify.json') != read_json(attempt / 'verify-config.json')):
        raise ValueError('Require a current ready-for-review cleanup')
    extraction = task / 'private/extractions' / identifier(extraction_id)
    extraction_hash = checked(extraction)
    manifest = read_json(extraction / 'extraction-manifest.json')
    if not manifest.get('producer_session') or not manifest.get('plan'):
        raise ValueError('Require verification from a separate extraction session')
    return {'cleanup_id': cleanup['cleanup_id'], 'cleanup_sha256': cleanup_hash,
            'extraction_id': extraction_id, 'extraction_sha256': extraction_hash,
            'diagnostic_sha256': digest(task / 'verify-latest.json'),
            'metadata_sha256': digest(task / 'metadata.json'),
            'validation': {p.parent.name: checked(p.parent) for p in sorted((task / 'private/validations' / extraction_id).glob('*/artifact-hashes.json'))}}


def approved(store, task_id, review_id):
    directory = store.task(task_id) / 'private/reviews' / identifier(review_id)
    checked(directory)
    record = read_json(directory / 'review.json')
    if record['report']['verdict'] != 'approve':
        raise ValueError('Independent review did not approve preparation')
    if record['inputs'] != inputs(store, task_id, record['inputs']['extraction_id']):
        raise ValueError('Independent review is stale; run a new review session')
    return record, directory


def revision_evidence(store, task_id, review_id):
    task = store.task(task_id)
    directory = task / 'private/reviews' / identifier(review_id)
    checked(directory)
    record = read_json(directory / 'review.json')
    if record['report']['verdict'] != 'revise':
        raise ValueError('Extraction revision requires a revise verdict')
    if record['inputs'] != inputs(store, task_id, record['inputs']['extraction_id']):
        raise ValueError('Extraction revision evidence is stale')
    return record


def evidence_index(workspace):
    """Describe verified private copies for this disposable agent workspace."""
    root = '/workspace/'
    future = workspace / 'future'
    trees = []
    if future.exists():
        for tree in sorted(p for p in future.iterdir() if p.is_dir()):
            trees.append({'commit': tree.name, 'root': root + tree.relative_to(workspace).as_posix(),
                          'files': [root + p.relative_to(workspace).as_posix()
                                    for p in sorted(tree.rglob('*')) if p.is_file()]})

    def extraction(directory):
        manifest_path = directory / 'extraction-manifest.json'
        if not manifest_path.exists():
            return None
        manifest = read_json(manifest_path)
        prefix = root + directory.relative_to(workspace).as_posix() + '/'
        return {'manifest': prefix + 'extraction-manifest.json',
                'evaluation_config_location': prefix + 'extraction-manifest.json#/plan/evaluation_config',
                'evaluation_config': manifest.get('plan', {}).get('evaluation_config'),
                'artifacts': [{'future_original': root + 'future/' + item['commit'] + '/' + item['path'],
                               'selected_copy': prefix + item['stored_path'], 'path': item['path']}
                              for item in manifest['artifacts']]}

    def validations(directory):
        return [{'record': root + p.relative_to(workspace).as_posix(),
                 'configuration_location': root + p.relative_to(workspace).as_posix() + '#/report/configuration',
                 'evidence_files': [root + f.relative_to(workspace).as_posix()
                                    for f in sorted(p.parent.rglob('*')) if f.is_file()]}
                for p in sorted(directory.glob('*/validation.json'))]

    revision = workspace / 'revision'
    return {'future_trees': trees,
            'diagnostic_runtime_config': root + 'runtime.json',
            'diagnostic_result': root + 'diagnostic.json',
            'extraction': extraction(workspace / 'extraction'),
            'validations': validations(workspace / 'validations'),
            'revision': {'review': root + 'revision/review.json',
                         'extraction': extraction(revision / 'extraction'),
                         'validations': validations(revision / 'validations')} if revision.exists() else None}


def session(store, task_id, *, stage, command=None, adapter=None, auth_file=None,
            context=None, sandbox=None, extraction_id=None, revision_id=None,
            require_evaluation_config=False):
    """Every invocation creates a fresh session; only disposable private copies mount."""
    from .agents import CodexAdapter, provision_auth
    from .verification import load_config
    if stage not in ('review', 'extraction'):
        raise ValueError('Unknown private preparation stage')
    runner = sandbox or DockerSandbox(context=context)
    from .downloads import cache_task
    cache_task(runner, store, task_id)
    with store.operation(task_id), tempfile.TemporaryDirectory(dir=store.root) as temp:
        temp = Path(temp)
        task, draft = store.task(task_id), store.draft(task_id)
        config = load_config(draft / 'control/verify.json')
        identity = uuid4().hex
        if revision_id and stage != 'extraction':
            raise ValueError('Revision evidence is only for extraction')
        bound = inputs(store, task_id, extraction_id) if stage == 'review' else None
        revision = revision_evidence(store, task_id, revision_id) if revision_id else None
        # Extraction follows a completed producer, even if it later reports unavailable.
        cleanup = read_json(task / 'cleanup-latest.json')
        checked(task / 'cleanups' / identifier(cleanup['cleanup_id']))
        workspace, control, prompts = temp / 'workspace', temp / 'control', temp / 'inputs'
        workspace.mkdir(); control.mkdir(); prompts.mkdir()
        write_json(workspace / 'runtime.json', config)
        private = task / 'private'
        for name in ('base', 'future', 'diffs'):
            if (private / name).exists():
                copy_tree(private / name, workspace / name)
        # Verify preserved evidence before it informs producing/review decisions.
        expected = read_json(task / 'source-hashes.json')
        actual = {f'{name}/{p}': v for name in ('base', 'future', 'diffs')
                  if (workspace / name).exists() for p, v in inventory(workspace / name).items()}
        if actual != {p: v for p, v in expected.items() if p.split('/', 1)[0] in ('base', 'future', 'diffs')}:
            raise ValueError('Stored source evidence was modified')
        copy_tree(draft / 'workspace', workspace / 'cleaned')
        write_json(workspace / 'task.json', read_json(task / 'metadata.json'))
        write_json(workspace / 'cleanup.json', cleanup)
        write_json(workspace / 'diagnostic.json', read_json(task / 'verify-latest.json'))
        if bound:
            copy_tree(private / 'extractions' / extraction_id, workspace / 'extraction')
            validations = private / 'validations' / extraction_id
            if validations.exists():
                copy_tree(validations, workspace / 'validations')
        if revision:
            previous = revision['inputs']['extraction_id']
            revision_dir = workspace / 'revision'
            revision_dir.mkdir()
            write_json(revision_dir / 'review.json', revision)
            copy_tree(private / 'extractions' / previous, revision_dir / 'extraction')
            validations = private / 'validations' / previous
            if validations.exists():
                copy_tree(validations, revision_dir / 'validations')
        write_json(workspace / 'evidence-index.json', evidence_index(workspace))
        prompt = REVIEW if stage == 'review' else EXTRACTION
        (prompts / 'prompt.txt').write_text(prompt)
        if adapter:
            if command or not isinstance(adapter, CodexAdapter):
                raise ValueError('Supply explicit argv or the dedicated Codex adapter')
            command = adapter.stage(prompts, required_files=(
                'review.json' if stage == 'review' else 'selection.json',),
                require_extraction_config=stage == 'extraction' and require_evaluation_config)
        if not command:
            raise ValueError('Supply an isolated agent executable or Codex adapter')
        if auth_file:
            if not isinstance(adapter, CodexAdapter):
                raise ValueError('Only Codex may receive authentication')
            provision_auth(auth_file, prompts / 'auth.json')
        spec = SandboxSpec(config['image'], tuple(command), Limits(**config.get('limits', {})),
                           workspace=workspace, control=control, inputs=prompts,
                           network='bridge', retain_logs=False)
        with runner.container(spec) as container:
            container.start()
            timed_out = container.wait(spec.limits.timeout_seconds)
            state = container.inspect()
            if state['Running']:
                raise ValueError('Private agent still running')
            if timed_out or state.get('OOMKilled') or state['ExitCode']:
                raise ValueError('Private agent did not complete; no approval/extraction published')
        producer = {'session_id': identity, 'harness': adapter.name if adapter else 'explicit-command',
                    'model': adapter.model if adapter else 'unspecified', 'image_id': container.image_id,
                    'instructions_sha256': digest(prompts / 'prompt.txt'),
                    'cleanup_id': cleanup['cleanup_id']}
        cache_stats = getattr(container, 'cache_stats', None)
        if isinstance(cache_stats, dict):
            producer['download_cache'] = cache_stats
        if stage == 'extraction':
            if revision_id:
                if revision != revision_evidence(store, task_id, revision_id):
                    raise ValueError('Extraction revision evidence changed during session')
                producer['revision_review_id'] = revision_id
            plan = read_json(control / 'selection.json')
            validate_plan(plan, require_evaluation_config=require_evaluation_config)
            if plan['strategy'] == 'unavailable':
                staged = temp / 'unavailable'; staged.mkdir()
                write_json(staged / 'report.json', {'plan': plan, 'producer': producer})
                seal(staged, private / 'extraction-attempts' / identity)
                return {'session_id': identity, 'status': 'needs-review', 'plan': plan}
            return store._extract(task_id, plan['artifacts'], plan=plan, producer_session=producer)
        report = read_json(control / 'review.json')
        validate_review(report)
        from .cleanup import validate_report
        if report['verdict'] == 'approve':
            validate_report({'disposition': 'ready-for-review', 'summary': '; '.join(report['reasons']),
                'objective_preserved': report['objective_preserved'], 'changes': report['changes'],
                'environment': cleanup['report']['environment']}, cleanup['changed_paths'])
        if bound != inputs(store, task_id, extraction_id):
            raise ValueError('Review inputs changed during session')
        staged = temp / 'review'; staged.mkdir()
        write_json(staged / 'review.json', {'review_id': identity, 'inputs': bound,
                                          'producer': producer, 'report': report})
        seal(staged, private / 'reviews' / identity)
        return {'review_id': identity, **report}


def strings(value):
    return isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)


def validate_plan(plan, *, require_evaluation_config=False):
    if (not isinstance(plan, dict) or set(plan) - {'evaluation_config'} != {'strategy', 'rationale', 'dependencies',
            'instructions', 'limitations', 'artifacts'} or
            plan['strategy'] not in ('focused', 'fuller', 'unavailable') or
            any(not isinstance(plan[k], str) or not plan[k].strip() for k in ('rationale', 'instructions')) or
            not all(strings(plan[k]) for k in ('dependencies', 'limitations')) or
            not isinstance(plan['artifacts'], list) or
            (bool(plan['artifacts']) != (plan['strategy'] != 'unavailable'))):
        raise ValueError('Invalid private verification plan')
    if require_evaluation_config and plan['strategy'] != 'unavailable' and not isinstance(plan.get('evaluation_config'), dict):
        raise ValueError('Runnable private verification requires an evaluation_config object')
    if plan.get('evaluation_config') is None:
        if 'evaluation_config' in plan and plan['strategy'] != 'unavailable':
            raise ValueError('Private evaluation_config must be an object')
        return
    if 'evaluation_config' in plan:
        from .verification import load_config
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            write_json(path, plan['evaluation_config'])
            config = load_config(path, evaluation=True)
        if not any(c['kind'] == 'tests' for c in config['commands']) or any(c.get('expected_failure') for c in config['commands']):
            raise ValueError('Private evaluation config needs tests without exemptions')


def validate_review(report):
    if (not isinstance(report, dict) or set(report) != {'verdict', 'reasons', 'objective_preserved',
            'snapshot_reviewed', 'changes'} or report['verdict'] not in ('approve', 'revise', 'reject') or
            not strings(report['reasons']) or not report['reasons'] or
            any(not isinstance(report[k], bool) for k in ('objective_preserved', 'snapshot_reviewed')) or
            not isinstance(report['changes'], list)):
        raise ValueError('Invalid independent review report')
    if report['verdict'] == 'approve' and not (report['objective_preserved'] and report['snapshot_reviewed']):
        raise ValueError('Approval requires explicit integrity and leakage review')


def validate(store, task_id, extraction_id, config_path, *, future_commit=None, sandbox=None,
             command=None, adapter=None, auth_file=None, evaluator_timeout=180):
    """Run selected original bytes privately in a fresh sandbox; never grade a solution."""
    from .verification import load_config
    from .evaluation import execute_checks, assess_checks
    from .preparation import commit
    with store.operation(task_id), tempfile.TemporaryDirectory(dir=store.root) as temp:
        temp = Path(temp)
        task = store.task(task_id)
        extraction = task / 'private/extractions' / identifier(extraction_id)
        extraction_hash = checked(extraction)
        manifest = read_json(extraction / 'extraction-manifest.json')
        config = load_config(config_path, evaluation=True)
        paths = [str(relative(item['path'])) for item in manifest['artifacts']]
        if len(paths) != len(set(paths)):
            raise ValueError('Validation requires one source per destination path')
        if future_commit:
            future_commit = commit(future_commit)
            if future_commit not in read_json(task / 'metadata.json')['future_commits']:
                raise ValueError('Unknown future validation commit')
            source = task / 'private/future' / future_commit
            expected = read_json(task / 'source-hashes.json')
            prefix = f'future/{future_commit}/'
            if inventory(source) != {p[len(prefix):]: v for p, v in expected.items() if p.startswith(prefix)}:
                raise ValueError('Stored future state was modified')
        else:
            source = store.draft(task_id) / 'workspace'
        source_hashes = inventory(source)
        # Use the same overlay, workdir and exit-code semantics as actual evaluation.
        # The execution evidence is temporary; the sealed report records the outcome.
        runner = sandbox or DockerSandbox()
        from .downloads import cache_task
        cache_task(runner, store, task_id)
        staged = temp / 'validation'; staged.mkdir()
        baseline = execute_checks(source, temp / 'workspace', staged / 'baseline',
                                  extraction, manifest['artifacts'], [], config, config['image'], runner)
        assessment = None
        evaluator = None
        report = dict(baseline)
        adapted = None
        if baseline['verdict'] in ('fail', 'inconclusive'):
            if (command or adapter) and config.get('grading_type') != 'isolated-argv-v1':
                try:
                    metadata = read_json(task / 'metadata.json')
                    assessment, evaluator = assess_checks(
                        source, extraction, manifest['artifacts'], config, config['image'], runner,
                        baseline, temp, metadata['task_summary'], command=command, adapter=adapter,
                        auth_file=auth_file, evaluator_timeout=evaluator_timeout,
                        proposed_path=staged / 'assessment.json', provenance_path=staged / 'evaluator.json')
                    if assessment.get('behavior_failure_reason'):
                        report.update(verdict='fail', explanation=assessment['behavior_failure_reason'],
                                      failure_basis='source_assessment', candidate_evidence=assessment['candidate_evidence'])
                    elif assessment.get('error_reason'):
                        report.update(verdict='error', explanation=assessment['error_reason'])
                    elif assessment['inconclusive_reason']:
                        report.update(verdict='inconclusive', explanation=assessment['inconclusive_reason'])
                    elif assessment['adaptations']:
                        adapted = execute_checks(source, temp / 'adapted-workspace', staged / 'adapted',
                                                 extraction, manifest['artifacts'], assessment['adaptations'],
                                                 config, config['image'], runner)
                        report = dict(adapted)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    if (staged / 'evaluator.json').exists():
                        evaluator = read_json(staged / 'evaluator.json')
                    report.update(verdict='error', explanation='Independent assessment failed.',
                                  error_kind=type(exc).__name__)
            else:
                report.update(verdict='inconclusive',
                              explanation='A failing test command was not independently assessed; a harness failure cannot be ruled out.')
        report['configuration'] = config
        selected_checks = staged / ('adapted/selected-checks.json' if adapted is not None
                                    else 'baseline/selected-checks.json')
        if selected_checks.exists():
            report['selected_checks'] = read_json(selected_checks)
        record = {'extraction_id': extraction_id, 'extraction_sha256': extraction_hash,
                  'state': future_commit or 'cleaned-base', 'source_hashes': source_hashes,
                  'policy': VALIDATION_POLICY, 'baseline': baseline, 'assessment': assessment,
                  'evaluator': evaluator, 'adapted': adapted, 'report': report,
                  'interpretation': {'error': 'environment_failure', 'pass': 'passed',
                                     'fail': 'behavior_failure', 'inconclusive': 'inconclusive'}[report['verdict']],
                  'limitation': 'Future validation uses the original future tree without cleanup edits. '
                                'Base behavioral failure is evidence for review, never automatic approval. '
                                'Adaptation provenance is checked; behavioral equivalence requires independent review.'}
        write_json(staged / 'validation.json', record)
        identity = uuid4().hex
        seal(staged, task / 'private/validations' / extraction_id / identity)
        return {'validation_id': identity, **record}
