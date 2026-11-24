"""Task-scoped Unix broker. Its only capabilities are verify and latest report."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import socketserver
import tempfile
import threading

from mineval.core.artifacts import copy_tree, copy_worktree, digest, read_json, write_json
from mineval.core.sandbox import DockerSandbox, Limits, RunResult, SandboxSpec
from mineval.historical.verification import diagnose, load_config


class Broker(socketserver.UnixStreamServer):
    # Single request at a time; no caller-provided paths, flags or task IDs.
    timeout = 0.5


def request(action='verify', socket_path=None):
    path = socket_path or os.environ.get('MINEVAL_VERIFY_SOCKET', '/mineval-broker/verify.sock')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(path)
        client.sendall(json.dumps({'action': action}).encode() + b'\n')
        with client.makefile('rb') as stream:
            response = stream.readline(128 * 1024 * 1024)
    return json.loads(response)


@contextmanager
def broker(store, task_id, runner, agent, *, workspace=None, config_path=None,
           diagnostic_ids=None, image_reference=None):
    # /tmp keeps the Unix path under Linux's 108-byte sockaddr limit.
    with tempfile.TemporaryDirectory(prefix='mineval-broker-') as temp:
        path = Path(temp) / 'verify.sock'
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(5)
                try:
                    payload = json.loads(self.rfile.readline(1024))
                    if payload == {'action': 'verify'}:
                        response = diagnose(store, task_id, sandbox=runner, agent=agent, already_locked=True,
                                            workspace=workspace, config_path=config_path,
                                            project_image=(image_reference, agent.container.image_id)
                                            if image_reference is not None else None)
                        if diagnostic_ids is not None:
                            diagnostic_ids.append(response['diagnostic_id'])
                    elif payload == {'action': 'report'}:
                        response = read_json(store.task(task_id) / 'verify-latest.json')
                    else:
                        response = {'error': 'Only verify and report are supported'}
                except Exception as exc:
                    response = {'error': str(exc)}
                try:
                    self.wfile.write(json.dumps(response).encode() + b'\n')
                except (BrokenPipeError, ConnectionResetError):
                    pass
        with Broker(str(path), Handler) as server:
            path.chmod(0o600)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
            thread.start()
            try:
                yield Path(temp)
            finally:
                server.shutdown()
                thread.join()


def session(store, task_id, command=None, *, context=None, stage='cleanup', adapter=None, auth_file=None):
    """Run isolated cleanup or the legacy explicit private-extraction command."""
    if stage not in ('cleanup', 'extraction'):
        raise ValueError('Unknown preparation stage')
    runner = DockerSandbox(context=context)
    store.configure_runner(runner, task_id)
    with store.operation(task_id):
        draft = store.draft(task_id)
        config = load_config(draft / 'control' / 'verify.json')
        limits = Limits(**config.get('limits', {}))
        if stage == 'extraction':
            # Private review copy: extraction agent can see future implementation,
            # cleanup agent and future evaluated agents cannot.
            with tempfile.TemporaryDirectory(dir=store.root) as temp:
                workspace = Path(temp) / 'review'
                workspace.mkdir()
                task = store.task(task_id)
                from mineval.historical.repository import copy_futures
                copy_futures(task, workspace / 'future')
                copy_tree(task / 'private' / 'base', workspace / 'base')
                if (task / 'private' / 'diffs').exists():
                    copy_tree(task / 'private' / 'diffs', workspace / 'diffs')
                metadata = read_json(task / 'metadata.json')
                write_json(workspace / 'task.json', {key: metadata[key] for key in
                           ('task_id', 'repository', 'base_commit', 'task_summary', 'future_commits')})
                spec = SandboxSpec(config['image'], tuple(command), limits, workspace=workspace,
                                   network=config.get('network', 'bridge'), services=config.get('services', ()), retain_logs=False)
                result = runner.run(spec)
                # Preserve suggestions; host extract still validates selected source bytes.
                selection = workspace / 'selection.json'
                if selection.exists():
                    write_json(store.task(task_id) / 'private' / 'extraction-selection.json', read_json(selection))
                return result
        return cleanup_session(store, task_id, command, runner=runner, adapter=adapter, auth_file=auth_file)


def cleanup_session(store, task_id, command, *, runner, adapter=None, auth_file=None):
    """Caller owns the task lock. Mount only disposable historical tree/control copies."""
    from mineval.historical.cleanup import INSTRUCTIONS, restore_history, save_attempt
    from mineval.core.agents import CodexAdapter, FakeAdapter, POLICY, provision_auth
    import time
    draft = store.draft(task_id)
    # Keep publication renames on the store filesystem; only the disposable child
    # workspace/control directories are mounted, never the store itself.
    with tempfile.TemporaryDirectory(prefix='.cleanup-', dir=store.root) as temp:
        temp = Path(temp)
        workspace, control, inputs = temp / 'workspace', temp / 'control', temp / 'inputs'
        input_hashes = copy_tree(draft / 'workspace', workspace)
        restore_history(store, task_id, workspace)
        control.mkdir(); inputs.mkdir()
        config = load_config(draft / 'control/verify.json')
        write_json(control / 'verify.json', config)
        summary = read_json(store.task(task_id) / 'metadata.json')['task_summary']
        brief = draft / 'control/task-brief.md'
        from mineval.historical.intake import read_brief
        (inputs / 'prompt.txt').write_text(POLICY + '\n' + INSTRUCTIONS + '\nTask to preserve:\n' +
                                           (read_brief(brief) if brief.exists() else summary))
        write_json(control / 'task.json', {'task_summary': summary})
        if adapter:
            if command or not isinstance(adapter, (CodexAdapter, FakeAdapter)):
                raise ValueError('Use explicit argv or a dedicated adapter')
            command = (adapter.stage(inputs, required_files=('cleanup-report.json', 'verify.json'))
                       if isinstance(adapter, CodexAdapter) else adapter.stage(inputs))
        if not command:
            raise ValueError('Supply a cleanup command or adapter')
        if auth_file:
            if not isinstance(adapter, CodexAdapter):
                raise ValueError('Only Codex may receive authentication')
            provision_auth(auth_file, inputs / 'auth.json')
        limits = Limits(**config.get('limits', {}))
        # The broker is attached after creating the container but before starting it.
        # Use a handle proxy so the broker can pause the current prep container.
        class Agent:
            container = None
            def pause(self):
                self.container.pause()
            def unpause(self):
                self.container.unpause()
        agent = Agent()
        diagnostic_ids = []
        with broker(store, task_id, runner, agent, workspace=workspace,
                    config_path=control / 'verify.json', diagnostic_ids=diagnostic_ids,
                    image_reference=config['image']) as directory:
            spec = SandboxSpec(config['image'], tuple(command), limits,
                               workspace=workspace, control=control, broker=directory, inputs=inputs,
                               network=config.get('agent_network', 'bridge'), workdir=config.get('workdir', '/workspace'),
                               services=config.get('services', ()), retain_logs=False)
            with runner.container(spec) as container:
                agent.container = container
                container.start()
                start = time.monotonic()
                timed_out = container.wait(limits.timeout_seconds)
                state = container.inspect()
                if state['Running']:
                    raise ValueError('Cleanup container still running')
                result = RunResult(container.id, container.image_id, state['ExitCode'], timed_out,
                                   state.get('OOMKilled', False), '', '', False, time.monotonic() - start)
        # Discard untrusted Git without invoking host Git on agent-writable metadata.
        clean = temp / 'clean'
        copy_worktree(workspace, clean)
        producer = ({'harness': adapter.name, 'model': adapter.model} if adapter else
                    {'harness': 'explicit-command', 'model': 'unspecified'})
        cache_stats = getattr(container, 'cache_stats', None)
        if isinstance(cache_stats, dict):
            producer['download_cache'] = cache_stats
        save_attempt(store, task_id, clean, control, result,
                     image_id=container.image_id, diagnostic_ids=diagnostic_ids,
                     producer=producer,
                     input_hashes=input_hashes, instructions_sha256=digest(inputs / 'prompt.txt'))
        return result
