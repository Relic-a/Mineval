"""Export exact historical Git trees."""
from mineval.core import processes as subprocess
from mineval.core.artifacts import inventory, relative
from pathlib import Path
import os
import tempfile


def export_git(repo, sha, target, *, submodule=None, validate=True, budget=None):
    from mineval.core.history import git
    if budget is None:
        budget = {'entries': 0, 'bytes': 0}
    if git(repo, 'rev-parse', '--verify', sha + '^{commit}').decode().strip() != sha:
        raise ValueError('Commit identity mismatch')
    # Read blobs directly: git archive honors export-ignore and could omit task files.
    entries = []
    from mineval.core.artifacts import MAX_BYTES, MAX_FILES
    for entry in git(repo, 'ls-tree', '-rz', '--full-tree', sha).split(b'\0'):
        if not entry:
            continue
        header, name = entry.split(b'\t', 1)
        mode, kind, oid = header.split()
        try:
            path = relative(name.decode('utf-8'))
        except UnicodeError as exc:
            raise ValueError(f'Unsupported non-UTF-8 Git path: {name!r}') from exc
        budget['entries'] += 1
        if budget['entries'] > MAX_FILES:
            raise ValueError(f'Repository exceeds {MAX_FILES} entries at {path}')
        if mode == b'160000' and kind == b'commit':
            if submodule is None:
                raise ValueError(f'Missing pinned submodule source: {path} at {oid.decode()}')
            submodule(repo, sha, path, oid.decode(), target, budget)
            continue
        if mode not in (b'100644', b'100755', b'120000') or kind != b'blob':
            raise ValueError(f'Unsupported Git entry: {path}')
        entries.append((mode, oid, path))
    # Bound the batch before exporting blob contents to temporary disk.
    from mineval.core.history import git_environment
    probe = subprocess.run(['git', '--no-replace-objects', '-C', str(repo), 'cat-file', '--batch-check'],
                           input=b''.join(oid + b'\n' for _, oid, _ in entries), env=git_environment(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    sizes = probe.stdout.splitlines()
    if probe.returncode or len(sizes) != len(entries):
        raise ValueError(f'Cannot inspect Git objects at {sha}')
    for (_, _, path), line in zip(entries, sizes):
        fields = line.split()
        if len(fields) != 3 or fields[1] != b'blob':
            raise ValueError(f'Missing Git blob at {sha}:{path}')
        budget['bytes'] += int(fields[2])
        if budget['bytes'] > MAX_BYTES:
            raise ValueError(f'Repository exceeds {MAX_BYTES} bytes at {sha}:{path}')
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.run(['git', '--no-replace-objects', '-C', str(repo), 'cat-file', '--batch'],
                              input=b''.join(oid + b'\n' for _, oid, _ in entries), stdout=out, env=git_environment(),
                              stderr=err, timeout=120)
        if proc.returncode:
            raise ValueError('Could not export Git objects')
        out.seek(0)
        total = 0
        for mode, oid, path in entries:
            object_id, kind, size = out.readline().split()
            size = int(size)
            total += size
            if object_id != oid or kind != b'blob' or total > MAX_BYTES:
                raise ValueError('Invalid or oversized Git export')
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if mode == b'120000':
                if size > 4095:
                    raise ValueError(f'Symbolic link exceeds 4095 bytes: {path}')
                destination.symlink_to(os.fsdecode(out.read(size)))
            else:
                with destination.open('xb') as dst:
                    remaining = size
                    while remaining:
                        block = out.read(min(remaining, 1024 * 1024))
                        if not block:
                            raise ValueError('Truncated Git object')
                        dst.write(block)
                        remaining -= len(block)
            if out.read(1) != b'\n':
                raise ValueError('Invalid Git object separator')
            if mode != b'120000':
                destination.chmod(0o755 if mode == b'100755' else 0o644)
    if validate:
        inventory(target)
