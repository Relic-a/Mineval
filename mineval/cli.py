"""Host preparation commands and a sandbox-side verification client."""

import argparse
from dataclasses import asdict
import json
import os
import subprocess
import sys

from .sandbox import DockerSandbox, Limits, SandboxError, SandboxSpec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mineval")
    parser.add_argument("--context", help="Docker context (otherwise use Docker's current selection)")
    parser.add_argument("--data", default=os.environ.get("MINEVAL_DATA", "mineval-data"))
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("doctor", help="verify rootless Docker and resource controller support")
    observe = commands.add_parser('observe', help='serve the private historical benchmarking dashboard')
    observe.add_argument('--port', type=int, default=0, help='loopback port (default: choose a free port)')
    observe.add_argument('--image', help='trusted preloaded default preparation image')
    observe.add_argument('--settings', help='trusted historical workflow settings JSON')
    observe.add_argument('--auth-file', help='explicit model-only credentials, never sent to the browser')
    arena = commands.add_parser('arena', help='persistent web design benchmarks and interactive preference ranking')
    arenas = arena.add_subparsers(dest='arena_action', required=True)
    arena_serve = arenas.add_parser('serve', help='open the private Web Arena workspace')
    arena_serve.add_argument('--image', required=True, help='trusted preloaded image with python3, node/npm and Codex')
    arena_serve.add_argument('--auth-file', help='explicit model-only Codex credential file')
    arena_serve.add_argument('--port', type=int, default=0)
    arena_serve.add_argument('--timeout', type=float, default=600)
    arenas.add_parser('create').add_argument('definition', help='JSON title/prompt/references/assets/starter')
    arenas.add_parser('list')
    arenas.add_parser('show').add_argument('benchmark')
    arena_run = arenas.add_parser('run', help='explicit new dated generation')
    arena_run.add_argument('benchmark')
    arena_run.add_argument('--image', required=True)
    arena_run.add_argument('--model', required=True)
    arena_run.add_argument('--effort', choices=('minimal','low','medium','high','xhigh'), default='medium')
    arena_run.add_argument('--adapter', choices=('codex','fixture'), default='fixture')
    arena_run.add_argument('--auth-file')
    arena_run.add_argument('--timeout', type=float, default=600)
    run = commands.add_parser("run", help="run an explicit command in an ephemeral sandbox")
    run.add_argument("--image", required=True, help="preloaded image reference; never pulled automatically")
    run.add_argument("--network", choices=("none", "bridge"), default="none")
    run.add_argument("--cpus", type=float, default=2)
    run.add_argument("--memory-mb", type=int, default=1024)
    run.add_argument("--pids", type=int, default=256)
    run.add_argument("--workspace-mb", type=int, default=512)
    run.add_argument("--tmp-mb", type=int, default=64)
    run.add_argument("--timeout", type=float, default=300)
    run.add_argument("command", nargs=argparse.REMAINDER)
    agent = commands.add_parser("agent-run", help="isolated Phase 3 adapter primitive; no benchmark records")
    agent.add_argument("workspace", help="sanitized materialized historical workspace")
    agent.add_argument("--instructions", required=True, help="task text file")
    agent.add_argument("--output", required=True, help="new output directory")
    agent.add_argument("--image", required=True, help="trusted preloaded image with git/runtime and adapter")
    agent.add_argument("--adapter", choices=("fake", "codex"), default="fake")
    agent.add_argument("--model", default="gpt-6-luna")
    agent.add_argument("--auth-file", help="explicit Codex auth.json only; never a home directory")
    agent.add_argument("--timeout", type=float, default=300)
    intake = commands.add_parser("import", help="register an externally selected candidate JSON")
    intake.add_argument("candidate")
    task = commands.add_parser("task", help="grow and inspect the local task pool")
    tasks = task.add_subparsers(dest="task_action", required=True)
    tasks.add_parser("add", help="register candidate JSON (alias of import)").add_argument("candidate")
    tasks.add_parser("list", help="list registered tasks")
    tasks.add_parser("show", help="inspect task metadata, state and record IDs").add_argument("task_id")
    retire = tasks.add_parser('retire', help='exclude future benchmarks, preserving historical records')
    retire.add_argument('task_id')
    retire.add_argument('--reason', required=True)
    revision = tasks.add_parser("revision", help="check and inspect an immutable prepared version")
    revision.add_argument("task_id")
    revision.add_argument("version")
    for name, field in (("run", "run_id"), ("evaluation", "evaluation_id")):
        record = tasks.add_parser(name, help=f"inspect a reserved {name} identity")
        record.add_argument("task_id")
        record.add_argument(field)
    suite = commands.add_parser("suite", help="immutable suites of explicit prepared versions")
    suites = suite.add_subparsers(dest="suite_action", required=True)
    suites.add_parser("create", help="publish a suite definition JSON").add_argument("definition")
    suites.add_parser("show", help="inspect a published suite").add_argument("suite_id")
    suites.add_parser("list", help="list published suites")
    bench = commands.add_parser("bench", help="isolated benchmarks and immutable attempt outputs")
    benches = bench.add_subparsers(dest="bench_action", required=True)
    benchmark = benches.add_parser('run', help='run one frozen task revision or an immutable suite')
    selection = benchmark.add_mutually_exclusive_group(required=True)
    selection.add_argument('--task')
    selection.add_argument('--suite')
    benchmark.add_argument('--version', help='exact frozen revision, required with --task')
    benchmark.add_argument('--run-id', help='new attempt ID (prefix for suite attempts)')
    benchmark.add_argument('--adapter', choices=('fake', 'codex'), default='fake')
    benchmark.add_argument('--model', help='explicit model; default fake or gpt-6-luna by adapter')
    benchmark.add_argument('--effort', choices=('minimal','low','medium','high','xhigh','max','ultra'))
    benchmark.add_argument('--auth-file')
    benchmark.add_argument('--timeout', type=float, help='override frozen environment timeout')
    benches.add_parser('list', help='list durable attempts').add_argument('--task')
    for name in ('show', 'recover', 'export'):
        attempt = benches.add_parser(name)
        attempt.add_argument('task_id')
        attempt.add_argument('run_id')
        if name == 'export':
            attempt.add_argument('destination', help='new directory outside the store; tree only')
    benches.add_parser('compare', help='report exact common revisions and coverage').add_argument('definition')
    evaluate = commands.add_parser('evaluate', help='independently assess a saved benchmark tree')
    evaluate.add_argument('run_id')
    evaluate.add_argument('--task', required=True)
    evaluate.add_argument('--config', required=True, help='trusted private verification commands JSON')
    evaluate.add_argument('--evaluation-id', help='new immutable assessment ID')
    evaluate.add_argument('--adapter', choices=('codex',))
    evaluate.add_argument('--model', default='gpt-6-luna')
    evaluate.add_argument('--auth-file')
    evaluate.add_argument('--command', nargs=argparse.REMAINDER, help='isolated evaluator argv')
    evaluations = commands.add_parser('evaluation', help='inspect and compare immutable assessments')
    assessments = evaluations.add_subparsers(dest='evaluation_action', required=True)
    show = assessments.add_parser('show')
    show.add_argument('task_id')
    show.add_argument('evaluation_id')
    assessments.add_parser('compare').add_argument('definition')
    verify = commands.add_parser("verify", help="fresh diagnostic via task-scoped host broker or host task ID")
    verify.add_argument("mode", nargs="?", choices=["report"])
    verify.add_argument("--task", help="host-only registered task ID; omit inside preparation sandbox")
    for action in ("prepare", "extraction-session"):
        session_parser = commands.add_parser(action, help="launch explicit argv in an isolated preparation sandbox")
        session_parser.add_argument("task_id")
        session_parser.add_argument("command", nargs=argparse.REMAINDER)
    cleanup = commands.add_parser('cleanup', help='minimally prepare a draft with an isolated dedicated adapter')
    cleanup.add_argument('task_id')
    cleanup.add_argument('--adapter', choices=('fake', 'codex'), default='fake')
    cleanup.add_argument('--model', default='gpt-6-luna')
    cleanup.add_argument('--auth-file')
    for name in ('verification-extract', 'review'):
        private = commands.add_parser(name, help='separate isolated private preparation agent')
        private.add_argument('task_id')
        if name == 'review':
            private.add_argument('--extraction-id', required=True)
        private.add_argument('--adapter', choices=('codex',))
        private.add_argument('--model', default='gpt-6-luna')
        private.add_argument('--auth-file')
        private.add_argument('--command', nargs=argparse.REMAINDER)
    validation = commands.add_parser('verification-check', help='privately diagnose extracted checks')
    validation.add_argument('task_id')
    validation.add_argument('--extraction-id', required=True)
    validation.add_argument('--config', required=True)
    validation.add_argument('--future-commit')
    freeze = commands.add_parser("finalize")
    freeze.add_argument("task_id")
    freeze.add_argument("--review-id", required=True, help="approved independent review session ID")
    reject = commands.add_parser("reject")
    reject.add_argument("task_id")
    reject.add_argument("--reason", required=True)
    extract = commands.add_parser("extract", help="privately preserve explicitly selected future verification")
    extract.add_argument("task_id")
    extract.add_argument("--selection", required=True, help="JSON array of commit/path/purpose/test_only entries")
    materialize = commands.add_parser("workspace", help="extract a frozen version into a new writable directory")
    materialize.add_argument("task_id")
    materialize.add_argument("version")
    materialize.add_argument("destination")
    workflow = commands.add_parser('workflow', help='historical preparation/run/evaluation queue shared with the dashboard')
    workflow.add_argument('--image', help='trusted preloaded default preparation image')
    workflow.add_argument('--settings', help='trusted reusable settings JSON')
    workflow.add_argument('--auth-file', help='explicit model-only credentials')
    flows = workflow.add_subparsers(dest='workflow_action', required=True)
    submit = flows.add_parser('submit', help='enqueue JSON {kind,selections,configurations,exclude}')
    submit.add_argument('definition')
    flows.add_parser('list')
    flows.add_parser('work', help='process queued tasks; independent failures do not stop the queue')
    retry = flows.add_parser('retry')
    retry.add_argument('job_id')
    retry.add_argument('--new-attempt', action='store_true', help='allocate a new run identity for an attempt without usable output')
    flows.add_parser('prepare-all', help='enqueue all eligible unprepared tasks')
    memory = commands.add_parser('model-review', help='append-only model memory and safe compaction reservations')
    memories = memory.add_subparsers(dest='memory_action', required=True)
    history = memories.add_parser('history')
    history.add_argument('model')
    history.add_argument('--archive', action='store_true')
    claim = memories.add_parser('claim', help='reserve oldest two active inputs, only above threshold')
    claim.add_argument('model')
    claim.add_argument('--ttl', type=int, default=900)
    memories.add_parser('inspect-claim').add_argument('reservation_id')
    commit = memories.add_parser('commit', help='commit JSON bullet array for a live reservation; retries are idempotent')
    commit.add_argument('reservation_id')
    commit.add_argument('summary')
    remember = memories.add_parser('record', help='append evidence-grounded review of a saved evaluation')
    remember.add_argument('task_id')
    remember.add_argument('evaluation_id')
    remember.add_argument('--bullets', help='JSON bullet array; omitted records the factual verdict and explanation')
    for name in ('review', 'compact'):
        operation = memories.add_parser(name, help='run a separate sandboxed review model')
        if name == 'review':
            operation.add_argument('task_id')
            operation.add_argument('evaluation_id')
        else:
            operation.add_argument('model_name')
        operation.add_argument('--image', required=True)
        operation.add_argument('--model', default='gpt-6-luna')
        operation.add_argument('--auth-file')
    args = parser.parse_args(argv)
    sandbox = DockerSandbox(context=args.context)
    try:
        if args.action == 'arena':
            from .arena import Arena
            from .artifacts import read_json
            arena = Arena(args.data)
            if args.arena_action == 'serve':
                from .arena_server import serve
                from .benchmark import termination_handler
                with termination_handler():
                    serve(args.data, args.image, context=args.context, auth_file=args.auth_file,
                          port=args.port, timeout=args.timeout)
                return 0
            if args.arena_action == 'create':
                result = arena.create(read_json(args.definition))
            elif args.arena_action == 'list':
                result = arena.records('benchmarks')
            elif args.arena_action == 'show':
                result = arena.summary(args.benchmark)
            else:
                from .arena_runtime import Runner
                from .artifacts import lock
                from .benchmark import termination_handler
                with lock(arena.root / 'server.lock'), termination_handler():
                    result = arena.enqueue(args.benchmark, [dict(harness=args.adapter, model=args.model, effort=args.effort)])[0]
                    result = Runner(arena, args.image, args.context, args.auth_file, args.timeout).generate(result['id'])
            print(json.dumps(result))
            return 125 if args.arena_action == 'run' and result['status'] != 'ready' else 0
        if args.action in ('workflow', 'model-review'):
            from .preparation import Store
            from .artifacts import read_json
            store = Store(args.data)
            if args.action == 'workflow':
                from .workflows import Workflows, tasks
                from .workflows import load_settings
                settings = load_settings(store, args.settings)
                if args.image:
                    settings['preparation_image'] = args.image
                flow = Workflows(store, settings, context=args.context, auth_file=args.auth_file)
                if args.workflow_action == 'submit':
                    definition = read_json(args.definition)
                    result = flow.enqueue(**definition)
                elif args.workflow_action == 'list':
                    result = flow.jobs()
                elif args.workflow_action == 'retry':
                    result = flow.retry(args.job_id, new_attempt=args.new_attempt)
                elif args.workflow_action == 'prepare-all':
                    result = flow.enqueue('prepare', [{'task_id': t['task_id']} for t in tasks(store)
                        if not t['versions'] and t['state']['status'] not in ('retired','rejected','needs-review')])
                else:
                    from .benchmark import termination_handler
                    with termination_handler():
                        flow.work()
                    result = flow.jobs()
            else:
                from .memory import Memory, record_evaluation
                memory = Memory(store)
                if args.memory_action == 'history':
                    result = memory.history(args.model, archive=args.archive)
                elif args.memory_action == 'claim':
                    result = memory.claim(args.model, ttl=args.ttl)
                elif args.memory_action == 'inspect-claim':
                    result = memory.inspect_claim(args.reservation_id)
                elif args.memory_action == 'commit':
                    result = memory.commit(args.reservation_id, read_json(args.summary))
                elif args.memory_action == 'record':
                    result = record_evaluation(store, args.task_id, args.evaluation_id,
                                               read_json(args.bullets) if args.bullets else None)
                else:
                    from .model_review import review_evaluation, compact
                    options = dict(image=args.image, model=args.model, context=args.context, auth_file=args.auth_file)
                    result = (review_evaluation(store, args.task_id, args.evaluation_id, **options)
                              if args.memory_action == 'review' else compact(store, args.model_name, **options))
            print(json.dumps(result, indent=2))
            return 0
        if args.action == 'observe':
            from .observation import serve
            from .preparation import Store
            from .workflows import Workflows
            from .artifacts import read_json
            from pathlib import Path
            store = Store(args.data)
            from .workflows import load_settings
            settings = load_settings(store, args.settings)
            if args.image:
                settings['preparation_image'] = args.image
            serve(store, port=args.port, workflows=Workflows(store, settings, context=args.context, auth_file=args.auth_file))
            return 0
        if args.action == "agent-run":
            from pathlib import Path
            from .agents import CodexAdapter, FakeAdapter, run_agent
            result = run_agent(
                args.workspace, Path(args.instructions).read_text(), args.output,
                image=args.image,
                adapter=CodexAdapter(args.model) if args.adapter == "codex" else FakeAdapter(),
                context=args.context, limits=Limits(timeout_seconds=args.timeout),
                auth_file=args.auth_file)
            print(json.dumps(result))
            return 0 if result['status'] == 'completed' else 124 if result['timed_out'] else 125
        if args.action not in ("doctor", "run"):
            from .artifacts import read_json
            from .intake import load_candidate
            from .preparation import Store
            from .service import request, session
            from .verification import diagnose
            if args.action == "verify" and not args.task:
                result = request(args.mode or "verify")
            else:
                store = Store(args.data)
                if args.action == 'evaluate':
                    from .evaluation import evaluate
                    from .agents import CodexAdapter
                    from .benchmark import termination_handler
                    with termination_handler():
                        result = evaluate(store, args.task, args.run_id, args.config,
                            evaluation_id=args.evaluation_id, sandbox=sandbox,
                            command=args.command, adapter=CodexAdapter(args.model) if args.adapter else None,
                            auth_file=args.auth_file)
                elif args.action == 'evaluation':
                    from .evaluation import compare
                    result = (store.inspect_evaluation(args.task_id, args.evaluation_id)
                              if args.evaluation_action == 'show' else compare(store, read_json(args.definition)))
                elif args.action == 'bench':
                    from . import benchmark
                    if args.bench_action == 'run':
                        from .agents import CodexAdapter, FakeAdapter
                        if bool(args.task) != bool(args.version):
                            raise ValueError('Use --task with --version, or --suite without --version')
                        adapter = (CodexAdapter(args.model or 'gpt-6-luna', effort=args.effort) if args.adapter == 'codex'
                                   else FakeAdapter(model=args.model or 'fake', effort=args.effort))
                        options = dict(run_id=args.run_id, adapter=adapter, context=args.context,
                                       auth_file=args.auth_file, timeout=args.timeout)
                        with benchmark.termination_handler():
                            result = (benchmark.run_suite(store, args.suite, **options) if args.suite else
                                      benchmark.run(store, args.task, args.version, **options))
                    elif args.bench_action == 'list':
                        result = benchmark.list_runs(store, args.task)
                    elif args.bench_action == 'show':
                        result = store.inspect_run(args.task_id, args.run_id)
                    elif args.bench_action == 'export':
                        result = benchmark.export(store, args.task_id, args.run_id, args.destination)
                    elif args.bench_action == 'recover':
                        result = benchmark.recover(store, args.task_id, args.run_id)
                    else:
                        result = benchmark.compare(store, read_json(args.definition))
                elif args.action == "import":
                    result = store.register(load_candidate(args.candidate))
                elif args.action == "task":
                    if args.task_action == "add":
                        result = store.register(load_candidate(args.candidate))
                    elif args.task_action == "list":
                        result = store.list_tasks()
                    elif args.task_action == "show":
                        result = store.inspect_task(args.task_id)
                    elif args.task_action == 'retire':
                        result = store.retire(args.task_id, args.reason)
                    elif args.task_action == "run":
                        result = store.inspect_run(args.task_id, args.run_id)
                    elif args.task_action == "evaluation":
                        result = store.inspect_evaluation(args.task_id, args.evaluation_id)
                    else:
                        result = store.inspect_revision(args.task_id, args.version)
                elif args.action == "suite":
                    if args.suite_action == "create":
                        result = store.create_suite(read_json(args.definition))
                    elif args.suite_action == "list":
                        result = store.list_suites()
                    else:
                        result = store.inspect_suite(args.suite_id)
                elif args.action == "verify":
                    result = (read_json(store.task(args.task) / 'verify-latest.json') if args.mode == 'report'
                              else diagnose(store, args.task, sandbox=sandbox))
                elif args.action == 'cleanup':
                    from .agents import CodexAdapter, FakeAdapter
                    result = asdict(session(store, args.task_id, context=args.context,
                                            adapter=CodexAdapter(args.model) if args.adapter == 'codex'
                                            else FakeAdapter(cleanup=True), auth_file=args.auth_file))
                    result['cleanup'] = read_json(store.task(args.task_id) / 'cleanup-latest.json')
                elif args.action in ("prepare", "extraction-session"):
                    command = args.command[1:] if args.command[:1] == ['--'] else args.command
                    if not command:
                        raise ValueError('Supply explicit executable argv after --; automatic agent preparation is not implemented')
                    result = asdict(session(store, args.task_id, command, context=args.context,
                                            stage='extraction' if args.action == 'extraction-session' else 'cleanup'))
                    if args.action == 'prepare':
                        result['cleanup'] = read_json(store.task(args.task_id) / 'cleanup-latest.json')
                elif args.action == 'verification-check':
                    from .review import validate
                    result = validate(store, args.task_id, args.extraction_id, args.config,
                                      future_commit=args.future_commit, sandbox=sandbox)
                elif args.action in ('verification-extract', 'review'):
                    from .review import session as private_session
                    from .agents import CodexAdapter
                    result = private_session(store, args.task_id,
                        stage='review' if args.action == 'review' else 'extraction',
                        command=args.command, adapter=CodexAdapter(args.model) if args.adapter else None,
                        auth_file=args.auth_file, context=args.context, extraction_id=getattr(args, 'extraction_id', None))
                elif args.action == "finalize":
                    result = store.finalize(args.task_id, review_id=args.review_id)
                elif args.action == "reject":
                    result = store.reject(args.task_id, args.reason)
                elif args.action == "extract":
                    result = store.extract(args.task_id, read_json(args.selection))
                elif args.action == "workspace":
                    result = store.materialize(args.task_id, args.version, args.destination)
            print(json.dumps(result, indent=2 if args.action in ('task', 'suite') else None))
            if args.action == 'evaluate':
                return {'pass': 0, 'fail': 1, 'inconclusive': 2, 'error': 125}[result['verdict']]
            if args.action == 'bench':
                if args.bench_action != 'run':
                    return 0
                records = result if isinstance(result, list) else [result]
                return (130 if any(r['status'] == 'cancelled' for r in records) else
                        124 if any(r['status'] == 'timeout' for r in records) else
                        125 if any(r['status'] != 'completed' for r in records) else 0)
            # Diagnostic failures are reports, never automatic candidate rejection.
            if args.action == 'verification-check':
                return 125 if result['interpretation'] == 'environment_failure' else 1 if result['interpretation'] == 'behavior_failure' else 0
            if args.action == 'verify':
                return 125 if result.get('error') or result.get('status') == 'environment_error' else 1 if result.get('status') == 'checks_failed' else 0
            if args.action in ('task', 'suite'):
                return 0
            return (124 if result.get('timed_out') else result.get('exit_code', 0)) or 0
        if args.action == "doctor":
            info = sandbox.check()
            print(json.dumps({"ready": True, "server_version": info["ServerVersion"]}))
            return 0
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        spec = SandboxSpec(args.image, tuple(command), Limits(
            cpus=args.cpus, memory_mb=args.memory_mb, pids=args.pids,
            workspace_mb=args.workspace_mb, tmp_mb=args.tmp_mb, timeout_seconds=args.timeout), network=args.network)
        result = sandbox.run(spec)
        print(json.dumps(asdict(result)))
        return 124 if result.timed_out else result.exit_code
    except (SandboxError, ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f"mineval: {exc}", file=sys.stderr)
        for note in getattr(exc, "__notes__", ()):
            print(note, file=sys.stderr)
        return 125
    except KeyboardInterrupt as exc:
        for note in getattr(exc, "__notes__", ()):
            print(note, file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
