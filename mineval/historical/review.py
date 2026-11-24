"""Private producing/review sessions and exact-input approval receipts."""
from pathlib import Path
from contextlib import ExitStack
import tempfile
from uuid import uuid4

from mineval.core.artifacts import copy_tree, digest, inventory, read_json, relative, write_json
from mineval.historical.sealed import checked, seal, strings
from mineval.core.store import identifier
from mineval.core.sandbox import DockerSandbox, Limits, SandboxSpec

VALIDATION_POLICY = "fixed-private-checks-v1"

EXTRACTION = """You are the separate verification extractor. Inspect task.json, base,
cleaned, future and diffs. Do not edit the task. Write /mineval-control/selection.json:
{strategy: focused|fuller|unavailable, rationale: nonempty string, dependencies:
[string], instructions: nonempty string, limitations: [string], artifacts:
[{commit, path, purpose, test_only: true} or {origin: generated, path, purpose, test_only: true, content}], evaluation_config: object}.
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
You may generate new behavioral check documents in selection.json using origin generated
and content, and combine them with historical artifacts. Generated checks must express
requirements in the original task, be independently reviewable, and be validated on
base and future before freezing. Do not copy production implementation into checks.
Use native-script-v1 for schema-version 2 candidates. For example, put this object
inside selection.json as evaluation_config, replacing the example values:
```json
{"grading_type": "native-script-v1", "image": "my-project:prepared",
 "workdir": "/workspace", "script": "verify.sh",
 "required_tests": ["tests.test_held_out::test_behavior"]}
```
Include services when runtime.json declares them. Omit commands entirely for native
grading; put any needed setup or build steps in the frozen script, which must exit
with the test runner's status. Preserve runtime-provided package cache environment
variables, especially NPM_CONFIG_CACHE: do not override a provided value with a
workspace-local cache. Use the seeded dependency caches for offline setup; choose a
fallback cache only when the runtime did not provide one. Do not require network
access to install dependencies that are already seeded. If task-brief.md is
absent, include a task_brief string with the full public task instructions in
selection.json. State required behavior, public interfaces, defaults, important
edge cases and behavior to preserve. Keep historical evidence, implementation
details and private check contents out of the brief. Include one regular .sh file
in artifacts and set
evaluation_config.script to its repository-relative path. The script explicitly
runs the normal project test runner against selected tests and writes JUnit XML to
"$MINEVAL_TEST_REPORT". Set required_tests to JUnit classname::name IDs (name only
if no classname). Use the same image, workdir and services as runtime.json. Include
held-out test files and fixtures in artifacts. Test public library interfaces directly
when appropriate. Check that the task brief describes the obligations tested.
For legacy HTTP-only grading use isolated-http-v1: service {argv, port}, network none,
setup commands followed by one tests marker. Each selected check is JSON {cases:
[{name, steps: [{method, path, optional headers, optional body, expect: {status,
optional body, json, headers}}]}]}. Requests execute in order and the trusted grader
compares responses outside the candidate sandbox. Use behavioral endpoints, not
internal implementation names. Include regression, setup, and cleanup requests when
needed; expected results must be fixed before any benchmark.
For legacy command-line grading supply evaluation_config with grading_type isolated-argv-v1,
image, network, optional limits, workdir, and commands. The single tests command is
executed against the candidate once for each private JSON case, passing its input as an
argument and comparing stdout with expected output. Selected private artifacts must be
JSON objects with a nonempty cases array containing input and expected strings. Private
case files are never mounted into the candidate container. Choose behavioral cases
that accept alternative implementations and exercise all required obligations. Check
that the full set executes; report coverage and validation limitations honestly.
Tests are fixed before benchmarks and must never be adapted to candidate code.
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
Read task-brief.md when present. Check that it describes the required behavior,
public interfaces, defaults, and material edge cases and invariants, without
prescribing an implementation or disclosing a solution. Check that each mandatory
test is supported by the brief and that material requirements are covered.
Review the actual evaluation_config and the private base/future validation
results identified in evidence-index.json. Approval binds the supplied validation
runs. Require selected tests to execute and finish. Detect skips and missing coverage
and assess relevance to the task and alternative legitimate implementations.
Record base execution limitations and coverage gaps. Passing on both trees is
nondiscriminating. Broken future checks and future environment failures remain
blockers. Do not approve misleading or unjustified commands or treat uncovered
obligations as tested. No outcome is automatic approval.
Write /mineval-control/review.json: {verdict: approve|revise|reject, reasons: [nonempty
strings], objective_preserved: boolean, snapshot_reviewed: boolean, changes:
[{path, reason}]}. Account for exactly each cleanup change. Approve only credible
verification and preserved tasks; explain unvalidated checks. You may inspect future
solutions here; never copy them into cleaned. No transcripts.
"""


def inputs(store, task_id, extraction_id, validation_selection=None):
    task, draft = store.task(task_id), store.draft(task_id)
    cleanup = read_json(task / 'cleanup-latest.json')
    attempt = task / 'cleanups' / identifier(cleanup['cleanup_id'])
    cleanup_hash = checked(attempt)
    current = inventory(draft / 'workspace')
    from mineval.historical.verification import load_config
    if (cleanup != read_json(attempt / 'cleanup-report.json') or
            cleanup['disposition'] != 'ready-for-review' or
            current != read_json(attempt / 'tree-hashes.json') or
            load_config(draft / 'control/verify.json') != read_json(attempt / 'verify-config.json')):
        raise ValueError('Require a current ready-for-review cleanup')
    extraction = task / 'private/extractions' / identifier(extraction_id)
    extraction_hash = checked(extraction)
    manifest = read_json(extraction / 'extraction-manifest.json')
    if not manifest.get('producer_session') or not manifest.get('plan'):
        raise ValueError('Require verification from a separate extraction session')
    brief = draft / 'control/task-brief.md'
    brief_receipt = {}
    if brief.exists() or brief.is_symlink():
        from mineval.historical.intake import read_brief
        read_brief(brief)
        brief_receipt = {'task_brief_sha256': digest(brief)}
    validations = task / 'private/validations' / extraction_id
    if validation_selection is None:
        receipts = {p.parent.name: checked(p.parent)
                    for p in sorted(validations.glob('*/artifact-hashes.json'))}
    else:
        metadata = read_json(task / 'metadata.json')
        states = {'base': 'cleaned-base'}
        if metadata.get('head_commit'):
            states['reference'] = metadata['head_commit']
        if set(validation_selection) != set(states):
            raise ValueError('Select a base validation and a reference validation when the task has a reference')
        receipts = {}
        for role, state in states.items():
            validation_id = identifier(validation_selection[role])
            directory = validations / validation_id
            if not (directory / 'artifact-hashes.json').is_file():
                raise ValueError(f'Unknown {role} validation for this extraction: {validation_id}')
            receipts[validation_id] = checked(directory)
            record = read_json(directory / 'validation.json')
            if (record['state'] != state or record['extraction_id'] != extraction_id
                    or record['extraction_sha256'] != extraction_hash):
                raise ValueError(f'Selected {role} validation does not match the review inputs')
            if role == 'base' and record['source_hashes'] != current:
                raise ValueError('Selected base validation did not run on the current cleaned base')
    return {**brief_receipt, 'cleanup_id': cleanup['cleanup_id'], 'cleanup_sha256': cleanup_hash,
            'extraction_id': extraction_id, 'extraction_sha256': extraction_hash,
            'diagnostic_sha256': digest(task / 'verify-latest.json'),
            'metadata_sha256': digest(task / 'metadata.json'),
            'validation': receipts,
            **({'validation_selection': validation_selection} if validation_selection is not None else {})}


def approved(store, task_id, review_id):
    directory = store.task(task_id) / 'private/reviews' / identifier(review_id)
    checked(directory)
    record = read_json(directory / 'review.json')
    if record['report']['verdict'] != 'approve':
        raise ValueError('Independent review did not approve preparation')
    bound = record['inputs']
    if bound != inputs(store, task_id, bound['extraction_id'], bound.get('validation_selection')):
        raise ValueError('Independent review is stale; run a new review session')
    return record, directory


def revision_evidence(store, task_id, review_id):
    task = store.task(task_id)
    directory = task / 'private/reviews' / identifier(review_id)
    checked(directory)
    record = read_json(directory / 'review.json')
    if record['report']['verdict'] != 'revise':
        raise ValueError('Extraction revision requires a revise verdict')
    bound = record['inputs']
    if bound != inputs(store, task_id, bound['extraction_id'], bound.get('validation_selection')):
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
                'artifacts': [{'future_original': (root + 'future/' + item['commit'] + '/' + item['path']) if item.get('origin') != 'generated' else None,
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
            require_evaluation_config=False, validation_selection=None):
    """Every invocation creates a fresh session; only disposable private copies mount."""
    from mineval.core.agents import CodexAdapter, provision_auth
    from mineval.historical.verification import load_config
    if stage not in ('review', 'extraction'):
        raise ValueError('Unknown private preparation stage')
    runner = sandbox or DockerSandbox(context=context)
    store.configure_runner(runner, task_id)
    with store.operation(task_id), tempfile.TemporaryDirectory(dir=store.root) as temp:
        temp = Path(temp)
        task, draft = store.task(task_id), store.draft(task_id)
        config = load_config(draft / 'control/verify.json')
        identity = uuid4().hex
        if revision_id and stage != 'extraction':
            raise ValueError('Revision evidence is only for extraction')
        if validation_selection is not None and stage != 'review':
            raise ValueError('Validation selection is only for independent review')
        bound = inputs(store, task_id, extraction_id, validation_selection) if stage == 'review' else None
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
        if (private / 'git-sources.json').exists():
            from mineval.historical.repository import copy_futures
            copy_futures(task, workspace / 'future')
        brief = draft / 'control/task-brief.md'
        if brief.exists() or brief.is_symlink():
            from mineval.historical.intake import read_brief
            (workspace / 'task-brief.md').write_text(read_brief(brief))
        # Verify preserved evidence before it informs producing/review decisions.
        expected = read_json(task / 'source-hashes.json')
        actual = {f'{name}/{p}': v for name in ('base', 'future', 'diffs')
                  if (workspace / name).exists() for p, v in inventory(workspace / name).items()}
        prefixes = ('base', 'diffs') if (private / 'git-sources.json').exists() else ('base', 'future', 'diffs')
        if ({p: v for p, v in actual.items() if p.split('/', 1)[0] in prefixes} !=
                {p: v for p, v in expected.items() if p.split('/', 1)[0] in prefixes}):
            raise ValueError('Stored source evidence was modified')
        copy_tree(draft / 'workspace', workspace / 'cleaned')
        write_json(workspace / 'task.json', read_json(task / 'metadata.json'))
        write_json(workspace / 'cleanup.json', cleanup)
        write_json(workspace / 'diagnostic.json', read_json(task / 'verify-latest.json'))
        if bound:
            copy_tree(private / 'extractions' / extraction_id, workspace / 'extraction')
            validations = private / 'validations' / extraction_id
            for validation_id in bound['validation']:
                copy_tree(validations / validation_id, workspace / 'validations' / validation_id)
        if revision:
            previous = revision['inputs']['extraction_id']
            revision_dir = workspace / 'revision'
            revision_dir.mkdir()
            write_json(revision_dir / 'review.json', revision)
            copy_tree(private / 'extractions' / previous, revision_dir / 'extraction')
            validations = private / 'validations' / previous
            for validation_id in revision['inputs']['validation']:
                copy_tree(validations / validation_id, revision_dir / 'validations' / validation_id)
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
        image = config['image']
        diagnostic = read_json(task / 'verify-latest.json')
        if diagnostic.get('image_id', '').startswith('sha256:') and len(diagnostic['image_id']) == 71:
            from mineval.historical.verification import pinned_image
            image = pinned_image(config, diagnostic)
        spec = SandboxSpec(image, tuple(command), Limits(**config.get('limits', {})),
                           workspace=workspace, control=control, inputs=prompts,
                           network=config.get('agent_network', 'bridge'), services=config.get('services', ()), retain_logs=False)
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
            result = store._extract(task_id, plan['artifacts'], plan=plan, producer_session=producer)
            if 'task_brief' in plan and not brief.exists():
                brief.write_text(plan['task_brief'])
            return result
        report = read_json(control / 'review.json')
        validate_review(report)
        from mineval.historical.cleanup import validate_report
        if report['verdict'] == 'approve':
            validate_report({'disposition': 'ready-for-review', 'summary': '; '.join(report['reasons']),
                'objective_preserved': report['objective_preserved'], 'changes': report['changes'],
                'environment': cleanup['report']['environment']}, cleanup['changed_paths'])
        if bound != inputs(store, task_id, extraction_id, validation_selection):
            raise ValueError('Review inputs changed during session')
        staged = temp / 'review'; staged.mkdir()
        write_json(staged / 'review.json', {'review_id': identity, 'inputs': bound,
                                          'producer': producer, 'report': report})
        seal(staged, private / 'reviews' / identity)
        return {'review_id': identity, **report}




def validate_plan(plan, *, require_evaluation_config=False):
    if (not isinstance(plan, dict) or set(plan) - {'evaluation_config', 'task_brief'} != {'strategy', 'rationale', 'dependencies',
            'instructions', 'limitations', 'artifacts'} or
            plan['strategy'] not in ('focused', 'fuller', 'unavailable') or
            any(not isinstance(plan[k], str) or not plan[k].strip() for k in ('rationale', 'instructions')) or
            not all(strings(plan[k]) for k in ('dependencies', 'limitations')) or
            not isinstance(plan['artifacts'], list) or
            (bool(plan['artifacts']) != (plan['strategy'] != 'unavailable'))):
        raise ValueError('Invalid private verification plan')
    if 'task_brief' in plan:
        brief = plan['task_brief']
        if not isinstance(brief, str) or not brief.strip() or len(brief.encode()) > 256 * 1024:
            raise ValueError('Task brief must be nonempty text of at most 256 KiB')
    if require_evaluation_config and plan['strategy'] != 'unavailable' and not isinstance(plan.get('evaluation_config'), dict):
        raise ValueError('Runnable private verification requires an evaluation_config object')
    if plan.get('evaluation_config') is None:
        if 'evaluation_config' in plan and plan['strategy'] != 'unavailable':
            raise ValueError('Private evaluation_config must be an object')
        return
    if 'evaluation_config' in plan:
        from mineval.historical.verification import load_config
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            write_json(path, plan['evaluation_config'])
            config = load_config(path, evaluation=True)
        if (config.get('grading_type') != 'native-script-v1' and not any(c['kind'] == 'tests' for c in config['commands'])) or any(c.get('expected_failure') for c in config['commands']):
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
    if command or adapter or auth_file:
        raise ValueError('AI-assisted validation is no longer supported')
    from mineval.historical.verification import load_config
    from mineval.historical.evaluation import execute_checks
    from mineval.core.store import commit
    with store.operation(task_id), tempfile.TemporaryDirectory(dir=store.root) as temp, ExitStack() as sources:
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
            from mineval.historical.repository import future_tree
            source = sources.enter_context(future_tree(task, future_commit))
        else:
            source = store.draft(task_id) / 'workspace'
        image = config['image']
        prepare_workspace = None
        if config.get('grading_type') == 'native-script-v1':
            from mineval.historical.native_grading import script_artifact
            from mineval.historical.verification import check_project_config, pinned_image
            script_artifact(manifest['artifacts'], config)
            project_config = load_config(store.draft(task_id) / 'control/verify.json')
            check_project_config(project_config, config)
            image = pinned_image(project_config, read_json(task / 'verify-latest.json'))
            if future_commit and (task / 'private/git-sources.json').exists():
                from mineval.historical.repository import attach_reference_history
                def prepare_workspace(workspace):
                    attach_reference_history(task, future_commit, workspace)
            else:
                from mineval.historical.repository import restore_history
                def prepare_workspace(workspace):
                    restore_history(store, task_id, workspace)
        source_hashes = inventory(source)
        # Use the same overlay, workdir and exit-code semantics as actual evaluation.
        # The execution evidence is temporary; the sealed report records the outcome.
        runner = sandbox or DockerSandbox()
        store.configure_runner(runner, task_id)
        staged = temp / 'validation'; staged.mkdir()
        baseline = execute_checks(source, temp / 'workspace', staged / 'baseline',
                                  extraction, manifest['artifacts'], [], config, image, runner,
                                  prepare_workspace=prepare_workspace, graders=store.graders)
        report = dict(baseline)
        report['configuration'] = config
        selected_checks = staged / 'baseline/selected-checks.json'
        if selected_checks.exists():
            report['selected_checks'] = read_json(selected_checks)
        record = {'extraction_id': extraction_id, 'extraction_sha256': extraction_hash,
                  'state': future_commit or 'cleaned-base', 'source_hashes': source_hashes,
                  'policy': VALIDATION_POLICY, 'baseline': baseline, 'report': report,
                  'interpretation': {'error': 'environment_failure', 'pass': 'passed',
                                     'fail': 'behavior_failure', 'inconclusive': 'inconclusive'}[report['verdict']],
                  'limitation': 'Future validation uses the original future tree without cleanup edits. '
                                'Base behavioral failure is evidence for review, never automatic approval. '
                                'Test relevance and completeness require independent review.'}
        write_json(staged / 'validation.json', record)
        identity = uuid4().hex
        seal(staged, task / 'private/validations' / extraction_id / identity)
        return {'validation_id': identity, **record}
