"""Reusable public npm tarballs; each sandbox receives a private writable copy.

Only SHA-512 payloads named by the preserved base lockfile may enter the shared
store. npm indexes, logs, credentials, model output and future lockfiles never do.
"""
import base64
import fcntl
from functools import wraps
import hashlib
import os
import re
from pathlib import Path
import shutil
import stat
import tempfile
from urllib.parse import urlsplit

from .artifacts import read_json


def synchronized(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / '.lock').open('a+b') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            return method(self, *args, **kwargs)
    return run


class NpmCache:
    max_bytes = 2 * 1024**3
    max_package_bytes = 256 * 1024**2

    def __init__(self, root, lockfile=None):
        self.root = Path(root)
        self.lockfile = Path(lockfile) if lockfile else None

    def manifest(self):
        """Snapshot the historical lockfile; never import agent-created entries."""
        if self.lockfile is None or not self.lockfile.is_file():
            return {}, {'reason': 'missing package-lock.json', 'unsupported_entries': 0}
        try:
            lock = read_json(self.lockfile)
        except (OSError, ValueError, TypeError):
            return {}, {'reason': 'unreadable package-lock.json', 'unsupported_entries': 0}
        if not isinstance(lock, dict) or not isinstance(lock.get('packages'), dict):
            return {}, {'reason': 'unsupported lockfile format', 'unsupported_entries': 0}
        result = {}
        unsupported = 0
        for name, package in lock['packages'].items():
            if name == '':  # root package is not a downloaded dependency
                continue
            if not isinstance(package, dict):
                unsupported += 1
                continue
            resolved, integrity = package.get('resolved'), package.get('integrity')
            try:
                url = urlsplit(resolved) if isinstance(resolved, str) else None
            except ValueError:
                url = None
            if (not url or url.scheme != 'https' or url.netloc != 'registry.npmjs.org'
                    or url.query or url.fragment or not isinstance(integrity, str)
                    or package.get('link')):
                unsupported += 1
                continue
            digests = []
            for value in integrity.split():
                if value.startswith('sha512-'):
                    try:
                        decoded = base64.b64decode(value[7:], validate=True)
                    except ValueError:
                        continue
                    if len(decoded) == 64:
                        digests.append(decoded.hex())
            if not digests:
                unsupported += 1
                continue
            version = package.get('version')
            if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9.+_-]{1,128}', version):
                version = None
            for digest in digests:
                result.setdefault(digest, []).append({
                    'version': version, 'registry': 'registry.npmjs.org',
                    'source_sha256': hashlib.sha256(resolved.encode()).hexdigest()})
        return result, {'reason': None, 'unsupported_entries': unsupported}

    def allowed(self):
        return set(self.manifest()[0])

    @staticmethod
    def relative(digest):
        return Path('_cacache/content-v2/sha512') / digest[:2] / digest[2:4] / digest[4:]

    @classmethod
    def payload(cls, root, digest):
        """Reject links and special files before reading any sandbox output."""
        current = Path(root)
        try:
            if not stat.S_ISDIR(current.lstat().st_mode):
                return None
            parts = cls.relative(digest).parts
            for part in parts[:-1]:
                current /= part
                if not stat.S_ISDIR(current.lstat().st_mode):
                    return None
            path = current / parts[-1]
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > cls.max_package_bytes:
                return None
            return path
        except FileNotFoundError:
            return None

    @staticmethod
    def matches(path, digest):
        h = hashlib.sha512()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(block)
        return h.hexdigest() == digest

    @synchronized
    def prepare(self):
        self.root.mkdir(parents=True, exist_ok=True)
        local = Path(tempfile.mkdtemp(prefix='.run-', dir=self.root))
        manifest, coverage = self.manifest()
        stats = {'manager': 'npm', 'seeded_packages': 0, 'seeded_bytes': 0,
                 'stored_packages': 0, 'eligible_packages': len(manifest),
                 'missed_packages': 0, 'unsupported_entries': coverage['unsupported_entries'],
                 'reason': coverage['reason'], 'artifacts': []}
        try:
            for digest in sorted(manifest):
                src = self.payload(self.root, digest)
                hit = src is not None and self.matches(src, digest)
                stats['artifacts'].append({'sha512': digest, 'sources': manifest[digest],
                                           'cache_hit': hit})
                if not hit:
                    stats['missed_packages'] += 1
                    continue
                dst = local / self.relative(digest)
                dst.parent.mkdir(parents=True, exist_ok=True)
                # Never hardlink writable sandbox files to the persistent store.
                shutil.copyfile(src, dst)
                os.utime(src, None)
                stats['seeded_packages'] += 1
                stats['seeded_bytes'] += src.stat().st_size
            return local, stats
        except BaseException:
            shutil.rmtree(local)
            raise

    @synchronized
    def collect(self, local, stats):
        # Use the pre-run snapshot, not a lockfile that may have changed.
        for entry in stats['artifacts']:
            digest = entry['sha512']
            src = self.payload(local, digest)
            if src is None or not self.matches(src, digest):
                continue
            dst = self.root / self.relative(digest)
            if dst.exists() and self.matches(dst, digest):
                os.utime(dst, None)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=dst.parent)
            try:
                with os.fdopen(fd, 'wb') as output, src.open('rb') as stream:
                    shutil.copyfileobj(stream, output)
                os.chmod(name, 0o444)
                os.replace(name, dst)
            finally:
                Path(name).unlink(missing_ok=True)
            stats['stored_packages'] += 1
        self._prune()

    def files(self):
        base = self.root / '_cacache/content-v2/sha512'
        return [p for p in base.glob('*/*/*') if p.is_file() and not p.is_symlink()]

    @synchronized
    def status(self):
        return self._status()

    def _status(self):
        files = self.files()
        return {'packages': len(files), 'bytes': sum(p.stat().st_size for p in files),
                'max_bytes': self.max_bytes}

    @synchronized
    def prune(self, max_bytes=None):
        return self._prune(max_bytes)

    def _prune(self, max_bytes=None):
        budget = self.max_bytes if max_bytes is None else max_bytes
        entries = sorted((p.stat().st_mtime, p.stat().st_size, p) for p in self.files())
        total = sum(size for _, size, _ in entries)
        removed = 0
        for _, size, path in entries:
            if total <= budget:
                break
            path.unlink(missing_ok=True)
            total -= size
            removed += 1
        return {'removed_packages': removed, **self._status()}


def cache_task(runner, store, task_id, *, version=None):
    from .sandbox import DockerSandbox
    if isinstance(runner, DockerSandbox):
        from .dependency_cache import DependencyCache
        snapshot = None
        if version is not None:
            from .preparation import identifier
            store._inspect_revision(task_id, version)
            snapshot = store.task(task_id) / 'versions' / identifier(version) / 'snapshot.tar.gz'
        runner.dependency_cache = DependencyCache(store.root / 'download-cache/artifacts',
                                                 store.task(task_id) / 'private/base', snapshot=snapshot)
    return runner
