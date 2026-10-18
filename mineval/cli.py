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
    intake = commands.add_parser("import", help="register an externally selected candidate JSON")
    intake.add_argument("candidate")
    verify = commands.add_parser("verify", help="fresh diagnostic via task-scoped host broker or host task ID")
    verify.add_argument("mode", nargs="?", choices=["report"])
    verify.add_argument("--task", help="host-only registered task ID; omit inside preparation sandbox")
    for action in ("prepare", "extraction-session"):
        session_parser = commands.add_parser(action, help="launch explicit argv in an isolated preparation sandbox")
        session_parser.add_argument("task_id")
        session_parser.add_argument("command", nargs=argparse.REMAINDER)
    freeze = commands.add_parser("finalize")
    freeze.add_argument("task_id")
    freeze.add_argument("--review", required=True, help="JSON review accounting for all cleanup changes")
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
    args = parser.parse_args(argv)
    sandbox = DockerSandbox(context=args.context)
    try:
        if args.action not in ("doctor", "run"):
            from .artifacts import read_json
            from .preparation import Store
            from .service import request, session
            from .verification import diagnose
            if args.action == "verify" and not args.task:
                result = request(args.mode or "verify")
            else:
                store = Store(args.data)
                if args.action == "import":
                    result = store.register(read_json(args.candidate))
                elif args.action == "verify":
                    result = (read_json(store.task(args.task) / 'verify-latest.json') if args.mode == 'report'
                              else diagnose(store, args.task, sandbox=sandbox))
                elif args.action in ("prepare", "extraction-session"):
                    command = args.command[1:] if args.command[:1] == ['--'] else args.command
                    result = asdict(session(store, args.task_id, command, context=args.context,
                                            stage='extraction' if args.action == 'extraction-session' else 'cleanup'))
                elif args.action == "finalize":
                    result = store.finalize(args.task_id, read_json(args.review))
                elif args.action == "reject":
                    result = store.reject(args.task_id, args.reason)
                elif args.action == "extract":
                    result = store.extract(args.task_id, read_json(args.selection))
                elif args.action == "workspace":
                    result = store.materialize(args.task_id, args.version, args.destination)
            print(json.dumps(result))
            # Diagnostic failures are reports, never automatic candidate rejection.
            if args.action == 'verify':
                return 125 if result.get('error') or result.get('status') == 'environment_error' else 1 if result.get('status') == 'checks_failed' else 0
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
