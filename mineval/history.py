"""Reconstruct history from an explicit base closure, never by cloning a source."""
import os
from pathlib import Path
import re
import shutil
from . import processes as subprocess

from .artifacts import MAX_BYTES


def git(repo, *args, input=None, output=None):
    # Do not inherit host Git config, replacement refs, credentials or object paths.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
               GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1')
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(repo), *args],
                            env=env, input=input, stdout=output or subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=120)
    if result.returncode:
        raise ValueError('Historical Git operation failed: ' + result.stderr[:2048].decode(errors='replace'))
    return result.stdout


def objects(repo, base):
    return {line.split()[0] for line in git(repo, 'rev-list', '--objects', base).splitlines()}


def create_pack(repo, base, destination):
    """Save only commits, trees and blobs reachable from base, including merge parents."""
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', base):
        raise ValueError('History requires a full base commit SHA')
    if git(repo, 'rev-parse', '--verify', base + '^{commit}').decode().strip() != base:
        raise ValueError('History base identity mismatch')
    if git(repo, 'rev-parse', '--is-shallow-repository').strip() != b'false':
        raise ValueError('Complete pre-base history is required; shallow sources are unsupported')
    closure = objects(repo, base)
    with Path(destination).open('xb') as out:
        git(repo, 'pack-objects', '--stdout', input=b'\n'.join(sorted(closure)) + b'\n', output=out)
    if Path(destination).stat().st_size > MAX_BYTES:
        Path(destination).unlink()
        raise ValueError('History exceeds artifact budget')
    # Validate by importing into a clean repository: no source config or alternates.
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        restore(destination, base, Path(temp) / 'check', checkout=False)


def restore(pack, base, workspace, *, checkout=False):
    """Attach sanitized history to a clean tree; preserve preparation edits by default."""
    if not isinstance(base, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', base):
        raise ValueError('History requires a full base commit SHA')
    if Path(pack).stat().st_size > MAX_BYTES:
        raise ValueError('History exceeds artifact budget')
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    if (workspace / '.git').exists() or (workspace / '.git').is_symlink():
        raise ValueError('Refusing existing Git metadata')
    try:
        git(workspace, 'init', '--template=', '--object-format=' + ('sha256' if len(base) == 64 else 'sha1'))
        data = Path(pack).read_bytes()
        if len(data) > MAX_BYTES:
            raise ValueError('History exceeds artifact budget')
        git(workspace, 'index-pack', '--stdin', input=data)
        git(workspace, 'update-ref', 'refs/heads/historical-base', base)
        git(workspace, 'symbolic-ref', 'HEAD', 'refs/heads/historical-base')
        git(workspace, 'read-tree', base)
        git(workspace, 'reflog', 'expire', '--expire=all', '--all')
        validate(workspace, base)
        if checkout:
            git(workspace, 'checkout-index', '-a', '-f')
    except BaseException:
        shutil.rmtree(workspace / '.git', ignore_errors=True)
        raise


def validate(workspace, base):
    """Check every stored object, not just refs, against the permitted base closure."""
    git(workspace, 'fsck', '--full', '--strict', '--no-reflogs')
    stored = set(git(workspace, 'cat-file', '--batch-all-objects', '--batch-check=%(objectname)').splitlines())
    if stored != objects(workspace, base):
        raise ValueError('Git contains objects outside the historical base closure')
    refs = git(workspace, 'for-each-ref', '--format=%(refname) %(objectname)').splitlines()
    if refs != [b'refs/heads/historical-base ' + base.encode()]:
        raise ValueError('Unexpected historical Git references')
