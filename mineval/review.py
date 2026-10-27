"""Private producing/review sessions and exact-input approval receipts."""
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from .artifacts import copy_tree, digest, inventory, read_json, write_json
from .preparation import identifier
from .sandbox import DockerSandbox, Limits, SandboxSpec

EXTRACTION = """You are the separate verification extractor. Inspect task.json, base,
cleaned, future and diffs. Do not edit the task. Write /mineval-control/selection.json:
{strategy: focused|fuller|unavailable, rationale: nonempty string, dependencies:
[string], instructions: nonempty string, limitations: [string], artifacts:
[{commit, path, purpose, test_only: true}]}. Select meaningful behavioral tests,
fixtures and essential test-only dependencies from the supplied history. Never
select production implementation as test support. Explain behavioral obligations,
not arbitrary implementation choices. If no credible checks exist, use unavailable
and an empty artifacts list. Do not invent superficial checks. No transcripts.
Also supply evaluation_config when runnable verification is available: a JSON object
with image (use the supplied runtime), network, optional limits, and commands
[{name, kind: setup|build|tests|smoke, argv: [strings]}]. Include a tests command
executing the selected private checks at /workspace paths. Distinguish behavioral
failure (exit 1) from inconclusive (2) and infrastructure errors (other codes).
Never exempt expected failures. This config is private, reviewed and frozen.
Omit it if no defensible runnable command exists; explain limitations.
"""
REVIEW = """You are the independent third reviewer, after cleanup and extraction.
Inspect task.json, base, cleaned, future/diffs, cleanup.json, diagnostic.json,
extraction and any validation results. Check task integrity, every cleanup edit,
future/credential leakage, test-only provenance, meaningful behavioral relevance,
arbitrary assumptions, alternate legitimate solutions, dependencies and reproducibility.
Review any evaluation_config against selected checks and exit-code semantics; do not
approve misleading or unjustified commands.
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


def session(store, task_id, *, stage, command=None, adapter=None, auth_file=None,
            context=None, sandbox=None, extraction_id=None):
    """Every invocation creates a fresh session; only disposable private copies mount."""
    from .agents import CodexAdapter, provision_auth
    from .verification import load_config
    if stage not in ('review', 'extraction'):
        raise ValueError('Unknown private preparation stage')
    runner = sandbox or DockerSandbox(context=context)
    with store.operation(task_id), tempfile.TemporaryDirectory(dir=store.root) as temp:
        temp = Path(temp)
        task, draft = store.task(task_id), store.draft(task_id)
        config = load_config(draft / 'control/verify.json')
        identity = uuid4().hex
        bound = inputs(store, task_id, extraction_id) if stage == 'review' else None
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
        for name in ('base', 'future', 'diffs'):
            if (workspace / name).exists():
                if {f'{name}/{p}': v for p, v in inventory(workspace / name).items()} != {
                        p: v for p, v in expected.items() if p.startswith(name + '/')}:
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
        prompt = REVIEW if stage == 'review' else EXTRACTION
        (prompts / 'prompt.txt').write_text(prompt)
        if adapter:
            if command or not isinstance(adapter, CodexAdapter):
                raise ValueError('Supply explicit argv or the dedicated Codex adapter')
            command = adapter.stage(prompts)
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
        if stage == 'extraction':
            plan = read_json(control / 'selection.json')
            validate_plan(plan)
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


def validate_plan(plan):
    if (not isinstance(plan, dict) or set(plan) - {'evaluation_config'} != {'strategy', 'rationale', 'dependencies',
            'instructions', 'limitations', 'artifacts'} or
            plan['strategy'] not in ('focused', 'fuller', 'unavailable') or
            any(not isinstance(plan[k], str) or not plan[k].strip() for k in ('rationale', 'instructions')) or
            not all(strings(plan[k]) for k in ('dependencies', 'limitations')) or
            not isinstance(plan['artifacts'], list) or
            (bool(plan['artifacts']) != (plan['strategy'] != 'unavailable'))):
        raise ValueError('Invalid private verification plan')
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


def validate(store, task_id, extraction_id, config_path, *, future_commit=None, sandbox=None):
    """Run selected original bytes privately in a fresh sandbox; never grade a solution."""
    from .verification import diagnose, load_config
    from .preparation import commit
    with store.operation(task_id), tempfile.TemporaryDirectory(dir=store.root) as temp:
        temp = Path(temp)
        task = store.task(task_id)
        extraction = task / 'private/extractions' / identifier(extraction_id)
        extraction_hash = checked(extraction)
        extraction_inventory = inventory(extraction)
        manifest = read_json(extraction / 'extraction-manifest.json')
        config = load_config(config_path, evaluation=True)
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
        workspace = temp / 'workspace'
        source_hashes = copy_tree(source, workspace)
        destinations = set()
        for item in manifest['artifacts']:
            from .artifacts import relative
            path = str(relative(item['path']))
            if path in destinations:
                raise ValueError('Validation requires one source per destination path')
            destinations.add(path)
            target = workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(extraction / relative(item['stored_path']), target)
            target.chmod(0o755 if extraction_inventory[item['stored_path']]['executable'] else 0o644)
        write_json(temp / 'verify.json', config)
        report = diagnose(store, task_id, sandbox=sandbox, already_locked=True,
                          workspace=workspace, config_path=temp / 'verify.json', persist=False)
        record = {'extraction_id': extraction_id, 'extraction_sha256': extraction_hash,
                  'state': future_commit or 'cleaned-base', 'source_hashes': source_hashes,
                  'report': report,
                  'interpretation': 'environment_failure' if report['status'] == 'environment_error' else
                    'passed' if report['status'] == 'completed' else 'behavior_failure',
                  'limitation': 'Future validation uses the original future tree without cleanup edits. '
                                'Base behavioral failure is evidence for review, never automatic approval.'}
        staged = temp / 'validation'; staged.mkdir()
        write_json(staged / 'validation.json', record)
        identity = uuid4().hex
        seal(staged, task / 'private/validations' / extraction_id / identity)
        return {'validation_id': identity, **record}
