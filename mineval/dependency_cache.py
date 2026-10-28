"""Host-owned, pinned download store and task-private package-manager inputs.

No package manager or project code is executed on the host. Only immutable,
public upstream archives are persisted. Native tools install into fresh task
workspaces; the shared store is never mounted and sandbox output is never imported.
"""
from dataclasses import dataclass
import base64
import gzip
import hashlib
from http.client import HTTPException
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import tempfile
import tomllib
from urllib.parse import unquote, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request
import zipfile
import zlib

from .downloads import synchronized

HOSTS = {'registry.npmjs.org', 'files.pythonhosted.org', 'pypi.org',
         'proxy.golang.org', 'static.crates.io'}
IGNORED = {'.git', 'node_modules', 'vendor', 'target', '.venv', 'venv', '__pycache__'}
MAX_BYTES = 256 * 1024**2
MAX_METADATA = 16 * 1024**2
MAX_ENTRIES = 20000
CARGO_CACHE_IDS = ('index.crates.io-1949cf8c6b5b557f', 'index.crates.io-6f17d22bba15001f',
                   'github.com-1ecc6299db9ec823')


def public_url(value):
    """An explicit allowlist also prevents credential forwarding and host SSRF."""
    try:
        url = urlsplit(value)
        return (url.scheme == 'https' and url.netloc in HOSTS and not url.query
                and not url.fragment and not url.username and not url.password
                and not any(c.isspace() or ord(c) < 32 for c in value))
    except (ValueError, TypeError):
        return False


class PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        # The public Go proxy may hand large ZIPs to its signed object store.
        # Accept that server-issued location only, never as a manifest source.
        go_storage = (req is not None and urlsplit(req.full_url).netloc == 'proxy.golang.org'
                      and target.scheme == 'https' and target.netloc == 'storage.googleapis.com'
                      and target.path.startswith('/proxy-golang-org-prod/') and not target.fragment
                      and not any(c.isspace() or ord(c) < 32 for c in newurl))
        if not public_url(newurl) and not go_storage:
            raise ValueError('Unsupported public download redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, limit=MAX_BYTES):
    if not public_url(url):
        raise ValueError('Unsupported public download source')
    # No host proxy configuration, netrc, cookies, credentials or user headers.
    opener = build_opener(ProxyHandler({}), PublicRedirects())
    with opener.open(Request(url, headers={'User-Agent': 'Mineval-download-cache/1'}), timeout=20) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Download exceeds cache limit')
    return data


def safe_file(root, relative):
    """Do not follow links in historical manifests or private staging inputs."""
    path = Path(root)
    try:
        if not stat.S_ISDIR(path.lstat().st_mode):
            return None
        parts = PurePosixPath(relative).parts
        if not parts or any(p in ('..', '/') for p in parts):
            return None
        for part in parts[:-1]:
            path /= part
            if not stat.S_ISDIR(path.lstat().st_mode):
                return None
        path /= parts[-1]
        info = path.lstat()
        return path if stat.S_ISREG(info.st_mode) and info.st_size <= MAX_BYTES else None
    except FileNotFoundError:
        return None


def hash1(files):
    """Go's golang.org/x/mod/sumdb/dirhash.Hash1 (names are significant)."""
    lines = []
    for name, data in sorted(files):
        if '\n' in name:
            raise ValueError('Invalid Go module filename')
        lines.append(hashlib.sha256(data).hexdigest() + '  ' + name + '\n')
    return 'h1:' + base64.b64encode(hashlib.sha256(''.join(lines).encode()).digest()).decode()


@dataclass(frozen=True)
class Artifact:
    manager: str
    name: str
    version: str
    url: str
    algorithm: str
    checksum: str
    destination: str
    manifest: str

    @property
    def key(self):
        # Include source and exact version, even if two sources serve equal bytes.
        value = [self.manager, self.version, self.url, self.algorithm, self.checksum]
        return hashlib.sha256(json.dumps(value, separators=(',', ':')).encode()).hexdigest()

    def verify(self, data):
        try:
            return self._verify(data)
        except (ValueError, OSError, RuntimeError, EOFError, zipfile.BadZipFile, zlib.error):
            return False

    def _verify(self, data):
        if self.algorithm == 'go-mod':
            return hash1([('go.mod', data)]) == self.checksum
        if self.algorithm == 'go-zip':
            prefix = self.name + '@' + self.version + '/'
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_BYTES:
                    return False
                files = []
                names = set()
                for info in infos:
                    name = info.filename
                    if (not name.startswith(prefix) or '..' in PurePosixPath(name).parts
                            or name in names or '\\' in name or '\n' in name):
                        return False
                    names.add(name)
                    if not info.is_dir():
                        files.append((name, archive.read(info)))
                return hash1(files) == self.checksum
        return hashlib.new(self.algorithm, data).hexdigest() == self.checksum


class DependencyCache:
    """Small format adapters around npm cacache, pip find-links, Go and Cargo.

    The historical tree must be host-owned. Do not pass an agent's live workspace.
    Acquisition is serialized with cleanup: concurrent cold requests download each
    pinned artifact once. Offline runs never initiate a host request.
    """
    max_bytes = 2 * 1024**3

    def __init__(self, root, historical_tree=None, *, snapshot=None):
        self.root = Path(root)
        self.tree = Path(historical_tree) if historical_tree else None
        self.snapshot = Path(snapshot) if snapshot else None
        self.issues = []
        self.artifacts = {}
        self.acquired = {}
        self.online = False

    def issue(self, manager, manifest, reason):
        entry = {'manager': manager, 'manifest': manifest, 'reason': reason}
        if entry not in self.issues:
            self.issues.append(entry)

    def add(self, manager, name, version, url, algorithm, checksum, destination, manifest):
        if (not public_url(url) or not re.fullmatch(r'[A-Za-z0-9_@.+!~/-]{1,512}', name)
                or not re.fullmatch(r'[A-Za-z0-9_.+!~-]{1,200}', version)
                or '..' in PurePosixPath(destination).parts or destination.startswith('/')):
            self.issue(manager, manifest, 'unsupported source or artifact identity')
            return
        if algorithm in ('sha256', 'sha512'):
            if not re.fullmatch('[0-9a-f]{' + str(hashlib.new(algorithm).digest_size * 2) + '}', checksum):
                self.issue(manager, manifest, 'missing strong integrity hash')
                return
        elif algorithm not in ('go-mod', 'go-zip') or not re.fullmatch(r'h1:[A-Za-z0-9+/]{43}=', checksum):
            self.issue(manager, manifest, 'invalid module checksum')
            return
        artifact = Artifact(manager, name, version, url, algorithm, checksum, destination, manifest)
        self.artifacts.setdefault(artifact.key, artifact)
        return artifact

    def manifests(self):
        if self.tree is None or not self.tree.is_dir():
            return []
        result = []
        for directory, dirs, files in os.walk(self.tree, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED and not (Path(directory) / d).is_symlink())
            for name in sorted(files):
                path = Path(directory) / name
                rel = path.relative_to(self.tree).as_posix()
                if safe_file(self.tree, rel):
                    result.append(rel)
        return result

    def text(self, manifest):
        path = safe_file(self.tree, manifest)
        if path is None or path.stat().st_size > MAX_METADATA:
            raise ValueError('Unsafe or oversized manifest')
        return path.read_text()

    def discover(self, managers):
        self.artifacts, self.issues = {}, []
        self.cargo_complete = True
        parsers = {'package-lock.json': ('npm', self.npm), 'npm-shrinkwrap.json': ('npm', self.npm),
                   'Cargo.lock': ('cargo', self.cargo), 'uv.lock': ('pip', self.uv),
                   'poetry.lock': ('pip', self.poetry), 'Pipfile.lock': ('pip', self.pipfile),
                   'pylock.toml': ('pip', self.pylock), 'go.mod': ('go', self.go)}
        manifests = self.manifests()
        known = set(manifests)
        for manifest in manifests:
            name = Path(manifest).name
            parser = parsers.get(name)
            if name.endswith(('.txt', '.in')) and ('requirements' in name or 'constraints' in name):
                parser = ('pip', self.requirements)
            if parser and parser[0] in managers:
                try:
                    parser[1](manifest)
                except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, zipfile.BadZipFile) as exc:
                    self.issue(parser[0], manifest, 'unreadable or unsupported manifest: ' + type(exc).__name__)
                    if parser[0] == 'cargo':
                        self.cargo_complete = False
            elif name in ('yarn.lock', 'pnpm-lock.yaml'):
                self.issue('other', manifest, 'lockfile format not supported; package manager may download normally')
            elif name in ('package.json', 'pyproject.toml', 'Cargo.toml'):
                manager, locknames = {
                    'package.json': ('npm', ('package-lock.json', 'npm-shrinkwrap.json')),
                    'pyproject.toml': ('pip', ('uv.lock', 'poetry.lock', 'pylock.toml', 'requirements.txt')),
                    'Cargo.toml': ('cargo', ('Cargo.lock',)),
                }[name]
                parents = [Path(manifest).parent, *Path(manifest).parents]
                if manager in managers and not any((parent / n).as_posix() in known for parent in parents for n in locknames):
                    self.issue(manager, manifest, 'manifest has no supported adjacent lock; native resolver may download')
                    if manager == 'cargo':
                        self.cargo_complete = False
        # A destination must never silently refer to two different sources/hashes.
        destinations = {}
        for key, artifact in list(self.artifacts.items()):
            previous = destinations.setdefault(artifact.destination, artifact)
            if previous.key != key:
                self.issue(artifact.manager, artifact.manifest, 'conflicting locked artifact destinations')
                if artifact.manager == 'cargo':
                    self.cargo_complete = False
                self.artifacts.pop(previous.key, None)
                self.artifacts.pop(key, None)
        return list(self.artifacts.values())

    def npm(self, manifest):
        lock = json.loads(self.text(manifest))
        packages = lock.get('packages')
        if not isinstance(packages, dict):
            # npm v1 has a recursive dependency tree.
            packages = {}
            def visit(deps, prefix=''):
                for name, value in deps.items():
                    key = prefix + 'node_modules/' + name
                    packages[key] = value
                    visit(value.get('dependencies', {}), key + '/')
            visit(lock.get('dependencies', {}))
        for name, package in packages.items():
            if name == '':
                continue
            integrity = package.get('integrity', '')
            valid = False
            for value in integrity.split():
                if not value.startswith(('sha512-', 'sha256-')):
                    continue
                algorithm, encoded = value.split('-', 1)
                try:
                    checksum = base64.b64decode(encoded, validate=True).hex()
                except ValueError:
                    continue
                if len(checksum) != hashlib.new(algorithm).digest_size * 2:
                    continue
                destination = f'npm/_cacache/content-v2/{algorithm}/{checksum[:2]}/{checksum[2:4]}/{checksum[4:]}'
                identity = package.get('name') or name.rsplit('node_modules/', 1)[-1]
                self.add('npm', identity, package.get('version', 'unknown'), package.get('resolved', ''),
                         algorithm, checksum, destination, manifest)
                valid = True
                break
            if not valid:
                self.issue('npm', manifest, 'unhashed, local, Git or unsupported npm dependency')

    def cargo(self, manifest):
        for package in tomllib.loads(self.text(manifest)).get('package', []):
            source = package.get('source')
            if source is None:  # workspace/path packages are never persisted
                continue
            if source != 'registry+https://github.com/rust-lang/crates.io-index':
                self.issue('cargo', manifest, 'Git or alternate Cargo registry is not cached')
                continue
            name, version = package['name'], package['version']
            filename = f'{name}-{version}.crate'
            artifact = self.add('cargo', name, version, f'https://static.crates.io/crates/{name}/{filename}',
                                'sha256', package.get('checksum', ''), 'cargo/archives/' + filename, manifest)
            if artifact is None:
                self.cargo_complete = False

    def wheel(self, manifest, name, version, wheel):
        url = wheel.get('url', '')
        filename = unquote(urlsplit(url).path.rsplit('/', 1)[-1])
        if not re.fullmatch(r'[A-Za-z0-9_.+!~-]+(?:\.whl|\.tar\.gz|\.zip)', filename):
            self.issue('pip', manifest, 'unsupported Python artifact filename')
            return
        checksum = wheel.get('hash', '').removeprefix('sha256:')
        self.add('pip', re.sub(r'[-_.]+', '-', name).lower(), version, url,
                 'sha256', checksum, 'pip/packages/' + filename, manifest)

    def uv(self, manifest):
        for package in tomllib.loads(self.text(manifest)).get('package', []):
            source = package.get('source', {})
            if (source.get('registry', '').rstrip('/') != 'https://pypi.org/simple'
                    and not public_url(source.get('url', ''))):
                self.issue('pip', manifest, 'local, Git or alternate uv source is not cached')
                continue
            for wheel in package.get('wheels', []):
                self.wheel(manifest, package['name'], package['version'], wheel)
            if package.get('sdist'):
                self.wheel(manifest, package['name'], package['version'], package['sdist'])
            if not package.get('wheels'):
                self.issue('pip', manifest, 'source build may download uncached build dependencies')

    def pylock(self, manifest):
        for package in tomllib.loads(self.text(manifest)).get('packages', []):
            if package.get('vcs') or package.get('directory'):
                self.issue('pip', manifest, 'VCS and directory packages are not cached')
                continue
            for wheel in package.get('wheels', []):
                self.wheel(manifest, package['name'], package['version'],
                           {'url': wheel.get('url', ''), 'hash': wheel.get('hashes', {}).get('sha256', '')})
            if package.get('sdist'):
                sdist = package['sdist']
                self.wheel(manifest, package['name'], package['version'],
                           {'url': sdist.get('url', ''), 'hash': sdist.get('hashes', {}).get('sha256', '')})
            if not package.get('wheels'):
                self.issue('pip', manifest, 'no locked wheel; source build may download dependencies')

    def pipfile(self, manifest):
        lock = json.loads(self.text(manifest))
        sources = lock.get('_meta', {}).get('sources', [])
        if not sources or any(s.get('url', '').rstrip('/') != 'https://pypi.org/simple' for s in sources):
            self.issue('pip', manifest, 'nondefault Pipenv index is not cached')
            return
        for group in ('default', 'develop'):
            for name, package in lock.get(group, {}).items():
                version = package.get('version', '')
                if not version.startswith('==') or any(k in package for k in ('git', 'path', 'file')):
                    self.issue('pip', manifest, 'unpinned or nonregistry Pipenv dependency')
                    continue
                self.pypi(manifest, name, version[2:],
                          {h[7:] for h in package.get('hashes', []) if h.startswith('sha256:')})

    def pypi(self, manifest, name, version, hashes):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or not re.fullmatch(r'[A-Za-z0-9_.+!-]+', version):
            self.issue('pip', manifest, 'unsupported Python requirement')
            return
        if not hashes:
            self.issue('pip', manifest, 'missing locked hashes')
            return
        # Only host-fetched public metadata is stored here. It never enters a task.
        url = f'https://pypi.org/pypi/{name}/{version}/json'
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self.root / 'metadata' / key
        try:
            cached = safe_file(self.root, 'metadata/' + key)
            metadata = None
            if cached and cached.stat().st_size <= MAX_METADATA:
                try:
                    metadata = json.loads(cached.read_bytes())
                    if not isinstance(metadata, dict) or not isinstance(metadata.get('urls'), list):
                        metadata = None
                except ValueError:
                    pass
            if metadata is None and self.online:
                data = download(url, MAX_METADATA)
                metadata = json.loads(data)
                if not isinstance(metadata, dict) or not isinstance(metadata.get('urls'), list):
                    raise ValueError('Invalid public index metadata')
                self.publish(path, data)
            elif metadata is None:
                self.issue('pip', manifest, 'PyPI artifact URLs unavailable with network disabled')
                return
            else:
                os.utime(path, None)
            found = False
            for file in metadata.get('urls', []):
                checksum = file.get('digests', {}).get('sha256')
                if checksum in hashes and file.get('packagetype') in ('bdist_wheel', 'sdist'):
                    self.wheel(manifest, name, version, {'url': file['url'], 'hash': checksum})
                    found = True
            if not found:
                self.issue('pip', manifest, 'no public archive matching locked hashes')
        except (OSError, ValueError, HTTPException):
            self.issue('pip', manifest, 'PyPI metadata unavailable or invalid')

    def poetry(self, manifest):
        lock = tomllib.loads(self.text(manifest))
        for package in lock.get('package', []):
            if package.get('source'):
                self.issue('pip', manifest, 'nondefault Poetry source is not cached')
                continue
            files = package.get('files', lock.get('metadata', {}).get('files', {}).get(package['name'], []))
            hashes = {f['hash'][7:] for f in files if f.get('hash', '').startswith('sha256:')}
            self.pypi(manifest, package['name'], package['version'], hashes)

    def requirements(self, manifest, seen=None):
        seen = set() if seen is None else seen
        if manifest in seen:
            return
        if len(seen) >= 128:
            self.issue('pip', manifest, 'requirements include depth exceeds cache limit')
            return
        seen.add(manifest)
        text = self.text(manifest).replace('\\\n', ' ')
        # Custom indexes can change the identity of a name/version. Never assume PyPI.
        if re.search(r'(?m)^\s*(--(?:extra-)?index-url|--find-links|-i\s|-f\s)', text):
            self.issue('pip', manifest, 'custom requirements index/find-links is not cached')
            return
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            include = re.fullmatch(r'(?:-r\s*|--requirement(?:=|\s+))([^\s]+)', line)
            if include:
                relative = Path(os.path.normpath(str(Path(manifest).parent / include[1]))).as_posix()
                try:
                    self.requirements(relative, seen)
                except (OSError, ValueError):
                    self.issue('pip', manifest, 'unsafe or missing requirements include')
                continue
            if line.startswith(('-c ', '--constraint', '--only-binary', '--require-hashes')):
                # Constraints alone do not authorize installing another dependency.
                continue
            hashes = set(re.findall(r'--hash[=\s]+sha256:([0-9a-f]{64})(?=\s|$)', line))
            requirement = re.split(r'\s+--hash', line)[0].split(';', 1)[0].strip()
            pinned = re.fullmatch(r'([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9_.+!-]+)', requirement)
            direct = re.fullmatch(r'([A-Za-z0-9_.-]+)\s*@\s*(https://\S+)', requirement)
            if pinned and hashes:
                self.pypi(manifest, pinned[1], pinned[2], hashes)
            elif direct:
                parsed = urlsplit(direct[2])
                fragment = re.fullmatch(r'sha256=([0-9a-f]{64})', parsed.fragment)
                hashes |= {fragment[1]} if fragment else set()
                url = parsed._replace(fragment='').geturl()
                filename = unquote(parsed.path.rsplit('/', 1)[-1])
                parts = filename.split('-')
                if len(hashes) == 1 and len(parts) >= 5:
                    self.wheel(manifest, direct[1], parts[1], {'url': url, 'hash': next(iter(hashes))})
                else:
                    self.issue('pip', manifest, 'direct wheel requires exact SHA-256')
            else:
                self.issue('pip', manifest, 'requirement is not pinned and hashed; normal resolver remains responsible')

    def go(self, manifest):
        text = self.text(manifest)
        if re.search(r'(?m)^\s*(replace|exclude)\b', text):
            self.issue('go', manifest, 'Go replace/exclude directives require normal module resolution')
            return
        sumfile = (Path(manifest).parent / 'go.sum').as_posix()
        sums = {}
        try:
            for line in self.text(sumfile).splitlines():
                fields = line.split()
                if len(fields) == 3:
                    sums[fields[0], fields[1]] = fields[2]
        except (OSError, ValueError):
            self.issue('go', manifest, 'missing historical go.sum')
            return
        # Walk only authenticated historical module metadata. go.sum is an
        # integrity authority, not a list of dependencies: unrelated old entries
        # never become roots. Native Go still performs version selection itself.
        pending = [(None, text)]
        visited = set()
        while pending:
            parent, module_text = pending.pop()
            if re.search(r'(?m)^\s*(replace|exclude)\b', module_text):
                self.issue('go', manifest, 'module graph contains replacement/exclusion; normal Go resolution may download')
            in_require = False
            for raw in module_text.splitlines():
                line = raw.split('//', 1)[0].strip()
                if line == 'require (':
                    in_require = True
                    continue
                if line == ')':
                    in_require = False
                    continue
                fields = (line[8:] if line.startswith('require ') else line if in_require else '').split()
                if len(fields) != 2:
                    continue
                name, version = fields
                if (name, version) in visited:
                    continue
                visited.add((name, version))
                if len(visited) > MAX_ENTRIES:
                    raise ValueError('Module graph exceeds cache limit')
                if not re.fullmatch(r'[A-Za-z0-9_.~/-]+', name) or not re.fullmatch(r'v[A-Za-z0-9_.+~-]+', version):
                    self.issue('go', manifest, 'unsupported Go module identity')
                    continue
                escape = lambda s: ''.join('!' + c.lower() if c.isupper() else c for c in s)
                base = escape(name) + '/@v/' + escape(version)
                for suffix, algorithm, sumversion in (('.mod', 'go-mod', version + '/go.mod'), ('.zip', 'go-zip', version)):
                    checksum = sums.get((name, sumversion))
                    if not checksum:
                        self.issue('go', manifest, 'reachable module lacks historical checksum; normal Go resolution may download')
                        continue
                    self.add('go', name, version, 'https://proxy.golang.org/' + base + suffix,
                             algorithm, checksum, 'go/cache/download/' + base + suffix, manifest)
                    if suffix == '.mod':
                        candidate = Artifact('go', name, version, 'https://proxy.golang.org/' + base + suffix,
                                             algorithm, checksum, 'go/cache/download/' + base + suffix, manifest)
                        if candidate.key not in self.artifacts:
                            continue
                        data, _, _, _ = self.acquire(candidate)
                        if data is not None:
                            pending.append(((name, version), data.decode('utf-8')))
                        else:
                            self.issue('go', manifest, 'module graph metadata unavailable; normal Go resolution may download')

    @staticmethod
    def publish(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o444)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def stored(self, artifact):
        path = safe_file(self.root, 'objects/' + artifact.key)
        if path:
            try:
                data = path.read_bytes()
                if artifact.verify(data):
                    return data
            except (OSError, ValueError, zipfile.BadZipFile):
                pass
        return None

    def acquire(self, artifact):
        if artifact.key in self.acquired:
            hit, downloaded, error = self.acquired[artifact.key]
            return self.stored(artifact), hit, downloaded, error
        data = self.stored(artifact)
        hit, downloaded, error = data is not None, False, None
        if (data is None and artifact.manager == 'npm' and artifact.algorithm == 'sha512'
                and urlsplit(artifact.url).netloc == 'registry.npmjs.org'):
            # Reuse the old host-managed store without exposing it or importing
            # any new sandbox output. Current authorization and hashes still apply.
            from .downloads import NpmCache
            legacy = NpmCache.payload(self.root.parent / 'npm', artifact.checksum)
            if legacy is not None:
                candidate = legacy.read_bytes()
                if artifact.verify(candidate):
                    data, hit = candidate, True
                    self.publish(self.root / 'objects' / artifact.key, data)
        if not hit and self.online:
            try:
                data = download(artifact.url, MAX_METADATA if artifact.algorithm == 'go-mod' else MAX_BYTES)
                if not artifact.verify(data):
                    raise ValueError('Artifact integrity mismatch')
                self.publish(self.root / 'objects' / artifact.key, data)
                downloaded = True
            except (OSError, ValueError, HTTPException, zipfile.BadZipFile) as exc:
                error, data = type(exc).__name__, None
        self.acquired[artifact.key] = (hit, downloaded, error)
        return data, hit, downloaded, error

    @synchronized
    def prepare(self, managers=('npm', 'pip', 'go', 'cargo'), *, network='none', cargo_cache_id=None):
        self.cargo_cache_ids = CARGO_CACHE_IDS
        if cargo_cache_id and re.fullmatch(r'[A-Za-z0-9_.-]+-[0-9a-f]{16}', cargo_cache_id):
            self.cargo_cache_ids = (cargo_cache_id,)
        if self.snapshot is None:
            return self._prepare(managers, network=network)
        from .artifacts import unpack
        # A frozen, receipt-checked preparation revision may authorize different
        # dependencies from the original base. Never use an active agent tree.
        with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=self.root) as temp:
            original = self.tree
            self.tree = Path(temp) / 'tree'
            try:
                unpack(self.snapshot, self.tree)
                return self._prepare(managers, network=network)
            finally:
                self.tree = original

    def _prepare(self, managers, *, network):
        self.online = network == 'bridge'
        self.acquired = {}
        artifacts = self.discover(set(managers))
        local = Path(tempfile.mkdtemp(prefix='.run-', dir=self.root))
        stats = {'schema_version': 2, 'seeded_packages': 0, 'seeded_bytes': 0,
                 'eligible_packages': len(artifacts), 'missed_packages': 0,
                 'stored_packages': 0, 'artifacts': [], 'unsupported': self.issues}
        if 'cargo' in managers:
            stats['cargo_cache_ids'] = list(self.cargo_cache_ids)
            stats['notes'] = ['Cargo registry metadata is task-local and may download. '
                              'Unrecognized Cargo cache layouts need an image cargo-cache-id label.']
        try:
            for artifact in artifacts:
                data, hit, downloaded, error = self.acquire(artifact)
                entry = {'manager': artifact.manager, 'name': artifact.name, 'version': artifact.version,
                         'source_sha256': hashlib.sha256(artifact.url.encode()).hexdigest(),
                         'registry': urlsplit(artifact.url).hostname, 'checksum': artifact.checksum,
                         'algorithm': artifact.algorithm, 'manifest': artifact.manifest, 'cache_hit': hit,
                         'available': False, 'downloaded': False}
                stats['artifacts'].append(entry)
                if not hit:
                    stats['missed_packages'] += 1
                if downloaded:
                    entry['downloaded'] = True
                    stats['stored_packages'] += 1
                if error:
                    entry['error_kind'] = error
                elif data is None and not self.online:
                    entry['reason'] = 'network disabled'
                if data is None:
                    continue
                entry['available'] = True
                path = local / artifact.destination
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                if artifact.manager == 'cargo':
                    for cache_id in self.cargo_cache_ids:
                        native = local / 'cargo/registry/cache' / cache_id / path.name
                        native.parent.mkdir(parents=True, exist_ok=True)
                        # Links are confined to this task-private staging tree.
                        # No inode is shared with persistent objects or other tasks.
                        os.link(path, native)
                if artifact.algorithm == 'go-zip':
                    path.with_suffix('.ziphash').write_text(artifact.checksum)
                    path.with_suffix('.info').write_text(json.dumps({'Version': artifact.version}))
                if hit:
                    os.utime(self.root / 'objects' / artifact.key, None)
                    stats['seeded_packages'] += 1
                    stats['seeded_bytes'] += len(data)
            for manager in managers:
                (local / manager).mkdir(exist_ok=True)
            if 'pip' in managers:
                (local / 'pip/packages').mkdir(parents=True, exist_ok=True)
            self.vendor(local, artifacts, stats)
            self._prune()
            return local, stats
        except BaseException:
            shutil.rmtree(local)
            raise

    def vendor(self, local, artifacts, stats):
        """Cargo's directory-source mechanism avoids sharing its mutable indexes.

        Extract only validated crates, never persist extracted sources/build output.
        Generate optional replacement only for a complete locked crates.io set.
        Never force it: agents may legitimately add dependencies after setup.
        """
        crates = [a for a in artifacts if a.manager == 'cargo']
        if not crates or not self.cargo_complete:
            return
        if any(not (local / a.destination).is_file() for a in crates):
            self.issue('cargo', '', 'incomplete crate set; Cargo downloads normally')
            return
        vendor = local / 'cargo/vendor'
        try:
            for artifact in crates:
                directory = vendor / (artifact.name + '-' + artifact.version)
                directory.mkdir(parents=True)
                files = {}
                total = 0
                # Bound decompression before tarfile parses PAX/GNU headers;
                # member-size checks alone do not bound oversized metadata.
                with gzip.open(local / artifact.destination, 'rb') as compressed:
                    expanded = compressed.read(MAX_BYTES + 1)
                if len(expanded) > MAX_BYTES:
                    raise ValueError('Expanded crate exceeds cache limit')
                with tarfile.open(fileobj=io.BytesIO(expanded), mode='r:') as archive:
                    for index, member in enumerate(archive):
                        total += member.size
                        parts = PurePosixPath(member.name).parts
                        if (index >= MAX_ENTRIES or total > MAX_BYTES or not parts
                                or parts[0] != directory.name or '..' in parts or '\\' in member.name
                                or not (member.isfile() or member.isdir())):
                            raise ValueError('Unsafe crate archive')
                        if member.isdir():
                            continue
                        relative = PurePosixPath(*parts[1:]).as_posix()
                        if relative in files or relative in ('.', '.cargo-checksum.json'):
                            raise ValueError('Conflicting crate member')
                        data = archive.extractfile(member).read()
                        target = directory / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                        if member.mode & 0o111:
                            target.chmod(0o755)
                        files[relative] = hashlib.sha256(data).hexdigest()
                (directory / '.cargo-checksum.json').write_text(json.dumps({'files': files, 'package': artifact.checksum}))
            (local / 'cargo/vendor-config.toml').write_text(
                '[source.crates-io]\nreplace-with = "mineval-locked"\n'
                '[source.mineval-locked]\ndirectory = "/mineval-dependency-cache/cargo/vendor"\n')
        except (OSError, ValueError, EOFError, zlib.error, tarfile.TarError):
            shutil.rmtree(vendor, ignore_errors=True)
            self.issue('cargo', '', 'crate cannot be safely staged; Cargo downloads normally')

    def files(self):
        return [p for p in (self.root / 'objects').glob('*')
                if re.fullmatch(r'[0-9a-f]{64}', p.name) and p.is_file() and not p.is_symlink()]

    def _status(self):
        files = self.files()
        metadata = [p for p in (self.root / 'metadata').glob('*') if p.is_file() and not p.is_symlink()]
        return {'packages': len(files), 'bytes': sum(p.stat().st_size for p in files),
                'metadata_bytes': sum(p.stat().st_size for p in metadata), 'max_bytes': self.max_bytes}

    @synchronized
    def status(self):
        return self._status()

    def _prune(self, max_bytes=None):
        budget = self.max_bytes if max_bytes is None else max_bytes
        if type(budget) is not int or budget < 0:
            raise ValueError('Cache budget must be a nonnegative integer')
        files = sorted((p.stat().st_mtime, p.stat().st_size, p) for p in self.files())
        size = sum(s for _, s, _ in files)
        removed = 0
        for _, length, path in files:
            if size <= budget:
                break
            path.unlink(missing_ok=True)
            size -= length
            removed += 1
        if budget == 0:
            shutil.rmtree(self.root / 'metadata', ignore_errors=True)
        else:
            metadata = sorted((p.stat().st_mtime, p.stat().st_size, p)
                              for p in (self.root / 'metadata').glob('*') if p.is_file() and not p.is_symlink())
            metadata_size = sum(s for _, s, _ in metadata)
            for _, length, path in metadata:
                if metadata_size <= min(budget, 128 * 1024**2):
                    break
                path.unlink(missing_ok=True)
                metadata_size -= length
        return {'removed_packages': removed, **self._status()}

    @synchronized
    def prune(self, max_bytes=None):
        return self._prune(max_bytes)
