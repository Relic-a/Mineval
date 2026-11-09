"""Small dedicated adapters. All agent execution is inside rootless Docker."""
from dataclasses import dataclass
from pathlib import Path
import shutil
import os
import tempfile
import time

from .artifacts import copy_tree, digest, inventory, pack, read_json, relative, write_json
from .history import git, validate
from .sandbox import DockerSandbox, Limits, SandboxSpec

POLICY = """Work only on the supplied task in /workspace.
Ordinary outbound web access is allowed. Do not look up or inspect this source
project's future implementation, commits, pull requests, historical solution,
or private verification. This is a trust policy, not a technical guarantee.
Generated node_modules directories without starting source are excluded from the
collected workspace; keep task source outside generated dependency directories.
At completion report whether you saw the historical solution, truthfully,
using historical_solution_seen: yes, no, or unknown. This is self-reported.
"""

SCHEMA = {'type': 'object', 'properties': {
    'summary': {'type': 'string'},
    'historical_solution_seen': {'type': 'string', 'enum': ['yes', 'no', 'unknown']}},
    'required': ['summary', 'historical_solution_seen'], 'additionalProperties': False}


@dataclass(frozen=True)
class CodexAdapter:
    model: str = 'gpt-6-luna'
    name: str = 'codex'
    effort: str | None = None

    def stage(self, inputs, *, required_files=(), require_extraction_config=False):
        if not self.model or self.model.startswith('-') or any(c.isspace() for c in self.model):
            raise ValueError('Supply an explicit model identifier')
        if self.effort not in (None, 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
            raise ValueError('Unsupported reasoning effort')
        required_files = tuple(required_files)
        for name in required_files:
            relative(name)
        write_json(inputs / 'required-files.json', list(dict.fromkeys(('final.json', *required_files))))
        if require_extraction_config:
            if 'selection.json' not in required_files:
                raise ValueError('Extraction config contract requires selection.json')
            write_json(inputs / 'extraction-contract.json', {'require_evaluation_config': True})
        shutil.copyfile(Path(__file__).with_name('codex_runner.py'), inputs / 'codex_runner.py')
        write_json(inputs / 'schema.json', SCHEMA)
        # No host binary, user config, plugins, home, or session directory is mounted.
        # Shell runs only in the container; task text is stdin, never shell source.
        command = ('sh', '-c', '''
set -eu
mkdir -p /tmp/codex
if [ -f /mineval-input/auth.json ]; then
  cp /mineval-input/auth.json /tmp/codex/auth.json
  chmod 600 /tmp/codex/auth.json
fi
npm_cache=/tmp/.npm
if [ -d /mineval-npm-cache ]; then npm_cache=/mineval-npm-cache; fi
dependency_env=''
if [ -d /mineval-dependency-cache/pip ]; then
  dependency_env="$dependency_env PIP_FIND_LINKS=/mineval-dependency-cache/pip/packages PIP_CACHE_DIR=/tmp/pip-cache"
fi
if [ -d /mineval-dependency-cache/go ]; then
  dependency_env="$dependency_env GOMODCACHE=/mineval-dependency-cache/go GOCACHE=/tmp/go-build"
fi
if [ -d /mineval-dependency-cache/cargo ]; then
  dependency_env="$dependency_env CARGO_HOME=/mineval-dependency-cache/cargo"
fi
exec env -i PATH="$PATH" HOME=/tmp CODEX_HOME=/tmp/codex RUSTUP_HOME="${RUSTUP_HOME:-/tmp/.rustup}" \
  $dependency_env \
  NPM_CONFIG_CACHE="$npm_cache" NPM_CONFIG_PREFER_OFFLINE=true NPM_CONFIG_AUDIT=false NPM_CONFIG_FUND=false \
  python3 /mineval-input/codex_runner.py --model "$1"
''', 'mineval-codex', self.model)
        if self.effort:
            return (*command[:2], command[2].replace('--model "$1"', '"$@"'), command[3],
                    '-c', 'model_reasoning_effort="' + self.effort + '"', '--model', self.model)
        return command


@dataclass(frozen=True)
class FakeAdapter:
    """Runs in the same container boundary as Codex; never executes on the host."""
    outcome: str = 'success'
    model: str = 'fake'
    name: str = 'fake'
    cleanup: bool = False
    effort: str | None = None

    def stage(self, inputs):
        if self.outcome not in ('success', 'error', 'timeout'):
            raise ValueError('Unknown fake outcome')
        write_json(inputs / 'fake.json', {'outcome': self.outcome, 'cleanup': self.cleanup})
        return ('python3', '-c', '''
import json, pathlib, time, subprocess
cfg = json.loads(pathlib.Path('/mineval-input/fake.json').read_text())
if cfg['outcome'] == 'timeout':
    time.sleep(86400)
if cfg['outcome'] == 'error':
    raise SystemExit(7)
assert pathlib.Path('/workspace/.git').is_dir()
assert pathlib.Path('/mineval-input/prompt.txt').read_text()
if cfg['cleanup']:
    subprocess.run(['python3', '-m', 'mineval', 'verify'], check=False, stdout=subprocess.DEVNULL)
    pathlib.Path('/mineval-control/cleanup-report.json').write_text(json.dumps({
        'disposition': 'ready-for-review', 'summary': 'Fake cleanup; no edits',
        'objective_preserved': True, 'changes': [], 'environment': {
            'runtime': 'Trusted fixture image', 'dependencies': [], 'services': [],
            'limitations': ['Fake adapter does not assess task integrity']}}))
else:
    pathlib.Path('/workspace/mineval-fake-result.txt').write_text('fake adapter completed\\n')
pathlib.Path('/mineval-control/final.json').write_text(json.dumps({
    'summary': 'Fake adapter completed', 'historical_solution_seen': 'no'}))
''')


def provision_auth(source, destination):
    """Explicit opt-in to one Codex credential file, never the host Codex directory."""
    value = read_json(source)
    if not isinstance(value, dict) or set(value) - {'auth_mode', 'OPENAI_API_KEY', 'tokens', 'last_refresh'}:
        raise ValueError('Unsupported Codex authentication file')
    if any(value.get(key) is not None and not isinstance(value[key], str)
           for key in ('auth_mode', 'OPENAI_API_KEY', 'last_refresh')):
        raise ValueError('Unsupported Codex authentication values')
    tokens = value.get('tokens')
    if tokens is not None and (not isinstance(tokens, dict) or
            set(tokens) - {'id_token', 'access_token', 'refresh_token', 'account_id'} or
            any(not isinstance(v, str) for v in tokens.values())):
        raise ValueError('Unsupported Codex token fields')
    if not value.get('OPENAI_API_KEY') and not (tokens and tokens.get('access_token')):
        raise ValueError('Codex authentication file has no credential')
    write_json(destination, value)
    destination.chmod(0o600)


def completion(path):
    report = read_json(path)
    if (not isinstance(report, dict) or set(report) != {'summary', 'historical_solution_seen'}
            or not isinstance(report['summary'], str) or len(report['summary']) > 16000
            or report['historical_solution_seen'] not in ('yes', 'no', 'unknown')):
        raise ValueError('Invalid agent completion report')
    return report


def discard_generated_npm(workspace, starting_inventory):
    """Remove only generated node_modules dirs without starting source, never links."""
    root = Path(workspace)
    protected = set()
    for name in starting_inventory:
        parts = Path(name).parts
        protected.update('/'.join(parts[:i + 1]) for i, part in enumerate(parts[:-1])
                         if part == 'node_modules')
    removed = 0
    for current, dirs, _ in os.walk(root, followlinks=False):
        for name in list(dirs):
            path = Path(current) / name
            if (name == 'node_modules' and path.relative_to(root).as_posix() not in protected
                    and not path.is_symlink()):
                shutil.rmtree(path)
                dirs.remove(name)
                removed += 1
    return removed


def run_agent(workspace, instructions, destination, *, image, adapter=None,
              context=None, limits=None, auth_file=None, sandbox=None, capture_interrupt=False, disposable=False, history_receipt=None):
    """Input is read-only to the host runner. Only disposable copies are mounted.

    Input must be a materialized historical workspace, never a modern clone.
    Output contains a tree-only snapshot (no agent Git config/hooks), plus status.
    Caller is responsible for reviewing input/output code for embedded secrets.
    """
    adapter = adapter or FakeAdapter()
    if not isinstance(adapter, (FakeAdapter, CodexAdapter)):
        raise ValueError('Only the dedicated fake and Codex adapters are supported')
    workspace, destination = Path(workspace).resolve(strict=True), Path(destination).absolute()
    if destination.exists() or destination == workspace or workspace in destination.parents:
        raise ValueError('Output must be a new directory outside the input workspace')
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError('Nonempty task instructions are required')
    if auth_file and not isinstance(adapter, CodexAdapter):
        raise ValueError('Only Codex may receive authentication')
    limits = limits or Limits()
    runner = sandbox or DockerSandbox(context=context)
    # Private store and input source directories are never mounted.
    with tempfile.TemporaryDirectory(prefix='mineval-agent-', dir=workspace.parent if disposable else None) as temp:
        temp = Path(temp)
        project, inputs, control = temp / 'workspace', temp / 'inputs', temp / 'control'
        inputs.mkdir(); control.mkdir()
        if disposable:
            os.rename(workspace, project)
        else:
            copy_tree(workspace, project, allow_git=True)
        # Reject external links and tampered metadata before invoking host Git.
        metadata = project / '.git'
        permitted = {'HEAD', 'config', 'description', 'index', 'objects', 'refs', 'logs', 'branches'}
        if not metadata.is_dir() or set(p.name for p in metadata.iterdir()) - permitted:
            raise ValueError('Input must have freshly reconstructed historical Git metadata')
        config = (metadata / 'config').read_text()
        # Exact config generated by init; forbid include paths, helpers and aliases.
        if any(line.strip() and line.strip() not in {
                '[core]', 'repositoryformatversion = 0', 'repositoryformatversion = 1',
                'filemode = true', 'bare = false', 'logallrefupdates = true',
                '[extensions]', 'objectformat = sha256'} for line in config.splitlines()):
            raise ValueError('Unexpected input Git configuration')
        if (metadata / 'objects' / 'info').exists() and any((metadata / 'objects' / 'info').iterdir()):
            raise ValueError('External Git object storage is forbidden')
        if history_receipt is not None:
            # Internal callers may supply the receipt from a provenance-checked
            # materialized template. Ordinary agent-run inputs still get full fsck.
            if inventory(metadata, allow_git=True) != history_receipt:
                raise ValueError('Prepared history receipt mismatch')
        else:
            base = git(project, 'rev-parse', 'HEAD').decode().strip()
            validate(project, base)
        starting_inventory = inventory(project, skip_root_git=True)
        (inputs / 'prompt.txt').write_text(POLICY + '\nTask:\n' + instructions)
        if auth_file:
            provision_auth(auth_file, inputs / 'auth.json')
        command = adapter.stage(inputs)
        spec = SandboxSpec(image, command, limits, workspace=project, inputs=inputs,
                           control=control, network='bridge', retain_logs=False)
        started = time.monotonic()
        try:
            with runner.container(spec) as container:
                container.start()
                timed_out = container.wait(limits.timeout_seconds)
                state = container.inspect()
                status = ('timeout' if timed_out else 'oom' if state.get('OOMKilled') else
                          'completed' if state['ExitCode'] == 0 else 'error')
                report = None
                if status == 'completed':
                    try:
                        report = completion(control / 'final.json')
                    except (ValueError, OSError, TypeError) as exc:
                        from .devlog import failure
                        failure("agent.completion_invalid", exc)
                        status = 'error'
                # Container must be stopped before inspecting any agent-writable path.
                if state['Running']:
                    raise ValueError('Agent container still running')
        except KeyboardInterrupt as exc:
            # The context manager removes the container before we read its tree.
            # Cleanup failure means writers might remain: never export in that case.
            if not capture_interrupt or getattr(exc, '__notes__', ()):
                raise
            state = {'ExitCode': None, 'OOMKilled': False}
            status, timed_out, report = 'cancelled', False, None
        # Never run host Git on agent-controlled output. Discard its Git metadata.
        if metadata.is_symlink() or metadata.is_file():
            metadata.unlink()
        elif metadata.exists():
            shutil.rmtree(metadata)
        from .devlog import event
        removed = discard_generated_npm(project, starting_inventory)
        event('agent.output.generated_dependencies_removed', directories=removed)
        inventory(project)  # Reject links/sockets/special files before exporting.
        destination.mkdir(parents=True, exist_ok=False)
        try:
            pack(project, destination / 'workspace.tar.gz')
            result = {'harness': adapter.name, 'model': adapter.model, 'status': status,
                      'exit_code': state['ExitCode'], 'timed_out': timed_out,
                      'oom_killed': state.get('OOMKilled', False),
                      'resources': dict(container.resources) if 'container' in locals() and isinstance(getattr(container, 'resources', None), dict) else {},
                      'image_id': container.image_id if 'container' in locals() else None,
                      'duration_seconds': time.monotonic() - started,
                      'instructions_sha256': digest(inputs / 'prompt.txt'),
                      'workspace_sha256': digest(destination / 'workspace.tar.gz'),
                      'completion': report, 'solution_lookup_evidence': 'self-reported'}
            cache_stats = getattr(container, 'cache_stats', None) if 'container' in locals() else None
            if isinstance(cache_stats, dict):
                result['download_cache'] = cache_stats
            write_json(destination / 'result.json', result)
        except BaseException:
            shutil.rmtree(destination)
            raise
        return result
