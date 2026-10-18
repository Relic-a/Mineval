"""Task-scoped Unix broker. Its only capabilities are verify and latest report."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import socketserver
import tempfile
import threading

from .artifacts import copy_tree, read_json, write_json
from .sandbox import DockerSandbox, Limits, SandboxSpec
from .verification import diagnose, load_config


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
def broker(store, task_id, runner, agent):
    # /tmp keeps the Unix path under Linux's 108-byte sockaddr limit.
    with tempfile.TemporaryDirectory(prefix='mineval-broker-') as temp:
        path = Path(temp) / 'verify.sock'
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(5)
                try:
                    payload = json.loads(self.rfile.readline(1024))
                    if payload == {'action': 'verify'}:
                        response = diagnose(store, task_id, sandbox=runner, agent=agent, already_locked=True)
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


def session(store, task_id, command, *, context=None, stage='cleanup'):
    """Run an explicitly supplied executable; no assumptions about agent protocol."""
    runner = DockerSandbox(context=context)
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
                copy_tree(task / 'private' / 'future', workspace / 'future')
                copy_tree(task / 'private' / 'base', workspace / 'base')
                if (task / 'private' / 'diffs').exists():
                    copy_tree(task / 'private' / 'diffs', workspace / 'diffs')
                metadata = read_json(task / 'metadata.json')
                write_json(workspace / 'task.json', {key: metadata[key] for key in
                           ('task_id', 'repository', 'base_commit', 'task_summary', 'future_commits')})
                spec = SandboxSpec(config['image'], tuple(command), limits, workspace=workspace,
                                   network=config.get('network', 'bridge'))
                result = runner.run(spec)
                # Preserve suggestions; host extract still validates selected source bytes.
                selection = workspace / 'selection.json'
                if selection.exists():
                    write_json(store.task(task_id) / 'private' / 'extraction-selection.json', read_json(selection))
                return result
        # The broker is attached after creating the container but before starting it.
        # Use a handle proxy so the broker can pause the current prep container.
        class Agent:
            container = None
            def pause(self):
                self.container.pause()
            def unpause(self):
                self.container.unpause()
        agent = Agent()
        with broker(store, task_id, runner, agent) as directory:
            spec = SandboxSpec(config['image'], tuple(command), limits,
                               workspace=draft / 'workspace', control=draft / 'control', broker=directory,
                               network=config.get('network', 'bridge'), workdir=config.get('workdir', '/workspace'))
            with runner.container(spec) as container:
                agent.container = container
                container.start()
                import time
                start = time.monotonic()
                timed_out = container.wait(limits.timeout_seconds)
                return container.result(timed_out=timed_out, duration_seconds=time.monotonic() - start)
