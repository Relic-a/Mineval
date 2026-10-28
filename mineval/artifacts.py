"""Bounded, link-free artifact operations. Never follow imported filesystem links."""
from contextlib import contextmanager
import fcntl
from functools import lru_cache
import hashlib
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile

MAX_BYTES = 512 * 1024 * 1024
MAX_FILES = 100_000


@lru_cache(maxsize=32768)
def _digest(path, identity):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    if _identity(Path(path).stat()) != identity:
        raise ValueError('Artifact changed while hashing')
    return h.hexdigest()


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def digest(path):
    # Cache bytes, not trust decisions: every call re-stats, every inventory still
    # checks membership, type and executable bits. ctime detects restored mtimes.
    path = Path(path).absolute()
    return _digest(str(path), _identity(path.stat()))


def clone_file(source, target):
    """Private inode, using filesystem CoW where available; never hardlink."""
    with open(source, 'rb') as src, open(target, 'wb') as dst:
        try:
            fcntl.ioctl(dst.fileno(), 0x40049409, src.fileno())  # FICLONE
        except OSError:
            shutil.copyfileobj(src, dst)
    shutil.copymode(source, target)
    return str(target)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, indent=2, sort_keys=True)
            f.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 128 * 1024 * 1024:
        raise ValueError('Invalid or oversized JSON artifact')
    return json.loads(path.read_text())


def relative(value):
    p = PurePosixPath(value)
    if not isinstance(value, str) or not value or p.is_absolute() or any(x in ('', '.', '..', '.git') for x in value.split('/')) or '\\' in value:
        raise ValueError(f'Unsafe artifact path: {value!r}')
    return p


def inventory(root, *, allow_git=False, skip_root_git=False):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Artifact root must be a directory, not a link')
    result = {}
    total = count = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        if skip_root_git and Path(current) == root:
            dirs[:] = [name for name in dirs if name != '.git']
            files[:] = [name for name in files if name != '.git']
        dirs.sort()
        files.sort()
        for name in dirs + files:
            count += 1
            if count > MAX_FILES:
                raise ValueError('Workspace exceeds artifact entry budget')
            path = Path(current) / name
            mode = path.lstat().st_mode
            if name == '.git' and not allow_git:
                raise ValueError('.git is forbidden in exported trees')
            if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                raise ValueError(f'Links and special files are unsupported: {path}')
            if stat.S_ISREG(mode):
                total += path.stat().st_size
                if total > MAX_BYTES or len(result) >= MAX_FILES:
                    raise ValueError('Workspace exceeds artifact budget')
                result[path.relative_to(root).as_posix()] = {'sha256': digest(path), 'executable': bool(mode & 0o111)}
    return result


def copy_tree(source, target, *, allow_git=False, expected_inventory=None):
    # The owner must quiesce writers. Detect accidental external changes as well.
    before = inventory(source, allow_git=allow_git)
    if expected_inventory is not None and before != expected_inventory:
        raise ValueError('Source integrity mismatch')
    shutil.copytree(source, target, copy_function=clone_file)
    if inventory(source, allow_git=allow_git) != before or inventory(target, allow_git=allow_git) != before:
        raise ValueError('Source changed during copy; stop external writers and retry')
    return before


def copy_worktree(source, target):
    """Copy an agent tree without reading or trusting its top-level Git metadata."""
    source = Path(source)
    before = inventory(source, skip_root_git=True)
    shutil.copytree(source, target, copy_function=clone_file, ignore=lambda directory, names:
                    {'.git'} if Path(directory) == source else set())
    if inventory(source, skip_root_git=True) != before or inventory(target) != before:
        raise ValueError('Source changed during copy; stop external writers and retry')
    return before


def unpack(archive, target):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=False)
    total = count = 0
    try:
        with tarfile.open(archive, 'r:*') as tar:
            seen = set()
            for member in tar:
                name = member.name.removeprefix('./').rstrip('/')
                if name in ('', '.') and member.isdir():
                    continue
                p = relative(name)
                if name in seen or not (member.isdir() or member.isfile()):
                    raise ValueError('Duplicate, linked or special archive member')
                seen.add(name)
                total += member.size
                count += 1
                if total > MAX_BYTES or count > MAX_FILES:
                    raise ValueError('Archive exceeds artifact budget')
                out = target.joinpath(*p.parts)
                if member.isdir():
                    out.mkdir(parents=True, exist_ok=True)
                else:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as src, out.open('xb') as dst:
                        shutil.copyfileobj(src, dst)
                    out.chmod(0o755 if member.mode & 0o111 else 0o644)
    except BaseException:
        shutil.rmtree(target)
        raise


def pack(source, target):
    inventory(source)
    def normalize(info):
        info.uid = info.gid = info.mtime = 0
        info.uname = info.gname = ''
        info.mode = 0o755 if info.isdir() or info.mode & 0o111 else 0o644
        return info
    with Path(target).open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as tar:
            for p in sorted(Path(source).rglob('*')):
                tar.add(p, arcname=p.relative_to(source).as_posix(), recursive=False, filter=normalize)


@contextmanager
def lock(path, *, wait=False, shared=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        try:
            fcntl.flock(f, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise ValueError('Task is busy in another preparation operation') from exc
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
