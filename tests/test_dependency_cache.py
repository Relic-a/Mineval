import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from mineval.artifacts import pack
from mineval.dependency_cache import Artifact, DependencyCache, PublicRedirects, hash1, public_url
from mineval.sandbox import DockerSandbox, SandboxSpec
from test_sandbox import READY, IMAGE_ID, CONTAINER_ID


class DependencyCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tree = self.root / 'historical'
        self.tree.mkdir()
        self.cache = DependencyCache(self.root / 'store', self.tree)
        self.downloads = {}
        self.fetch = patch('mineval.dependency_cache.download', side_effect=lambda url, *args: self.downloads[url]).start()
        self.addCleanup(patch.stopall)

    def write(self, name, text):
        path = self.tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def npm(self, name='one', data=b'one', manifest='package-lock.json', version='1.0.0'):
        url = f'https://registry.npmjs.org/{name}/-/{name}-{version}.tgz'
        self.downloads[url] = data
        integrity = 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode()
        self.write(manifest, json.dumps({'lockfileVersion': 3, 'packages': {
            f'node_modules/{name}': {'version': version, 'resolved': url, 'integrity': integrity}}}))
        return url

    def prepare(self, managers=('npm',), network='bridge'):
        local, stats = self.cache.prepare(managers, network=network)
        self.addCleanup(shutil.rmtree, local, True)
        return local, stats

    def test_nested_locks_scoped_packages_and_task_intersection(self):
        first = self.npm('one')
        second = self.npm('@scope/two', b'two', 'apps/web/package-lock.json')
        self.npm('ignored', b'ignored', 'node_modules/private/package-lock.json')
        local, stats = self.prepare()
        self.assertEqual(stats['stored_packages'], 2)
        self.assertEqual(self.fetch.call_count, 2)
        for p in local.rglob('*'):
            if p.is_file():
                p.write_bytes(b'agent poison')
        self.write('package-lock.json', '{}')
        self.npm('three', b'three', 'other/package-lock.json')
        _, later = self.prepare()
        self.assertEqual(later['seeded_packages'], 1)
        self.assertEqual(later['stored_packages'], 1)
        self.assertEqual(self.fetch.call_count, 3)
        self.assertNotIn('one', [e['name'].split('/')[-1] for e in later['artifacts']])
        self.assertTrue(all(e['available'] for e in later['artifacts']))

    def test_concurrent_cold_requests_fetch_once(self):
        url = self.npm()
        def prepare(_):
            return DependencyCache(self.cache.root, self.tree).prepare(('npm',), network='bridge')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(prepare, range(2)))
        for local, _ in results:
            self.addCleanup(shutil.rmtree, local, True)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(sum(s['stored_packages'] for _, s in results), 1)
        self.assertEqual(sum(s['seeded_packages'] for _, s in results), 1)

    def test_installation_paths_do_not_duplicate_npm_artifact_identity(self):
        self.npm()
        lock = json.loads((self.tree / 'package-lock.json').read_text())
        lock['packages']['node_modules/parent/node_modules/one'] = lock['packages']['node_modules/one']
        self.write('package-lock.json', json.dumps(lock))
        _, stats = self.prepare()
        self.assertEqual(stats['eligible_packages'], 1)
        self.assertEqual(self.fetch.call_count, 1)

    def test_legacy_npm_payload_is_reused_offline_after_validation(self):
        from mineval.downloads import NpmCache
        self.npm()
        checksum = hashlib.sha512(b'one').hexdigest()
        path = self.cache.root.parent / 'npm' / NpmCache.relative(checksum)
        path.parent.mkdir(parents=True); path.write_bytes(b'one')
        _, stats = self.prepare(network='none')
        self.assertEqual(stats['seeded_packages'], 1)
        self.fetch.assert_not_called()

    def test_integrity_failure_corruption_and_retry(self):
        url = self.npm()
        self.downloads[url] = b'bad download'
        _, failed = self.prepare()
        self.assertEqual(failed['stored_packages'], 0)
        self.assertEqual(self.cache.status()['packages'], 0)
        self.downloads[url] = b'one'
        self.prepare()
        stored = self.cache.files()[0]
        stored.chmod(0o600)
        stored.write_bytes(b'corruption')
        _, repaired = self.prepare()
        self.assertEqual(repaired['stored_packages'], 1)
        self.assertFalse(repaired['artifacts'][0]['cache_hit'])
        self.assertEqual(stored.read_bytes(), b'one')
        self.assertFalse(list(stored.parent.glob('tmp*')))

    def test_offline_never_acquires_and_prune_keeps_active_copies(self):
        self.npm()
        _, offline = self.prepare(network='none')
        self.fetch.assert_not_called()
        self.assertEqual(offline['artifacts'][0]['reason'], 'network disabled')
        local, _ = self.prepare()
        before = {p.relative_to(local): p.read_bytes() for p in local.rglob('*') if p.is_file()}
        self.cache.prune(0)
        self.assertEqual(self.cache.status()['packages'], 0)
        self.assertTrue(all((local / p).read_bytes() == data for p, data in before.items()))

    def test_symlink_manifests_and_cache_entries_are_not_followed(self):
        external = self.root / 'external'; external.mkdir()
        self.npm(manifest='nested/package-lock.json')
        shutil.move(self.tree / 'nested/package-lock.json', external)
        (self.tree / 'nested/package-lock.json').symlink_to(external / 'package-lock.json')
        (self.tree / 'link').symlink_to(external, target_is_directory=True)
        _, stats = self.prepare()
        self.assertEqual(stats['eligible_packages'], 0)
        self.npm()
        self.prepare()
        stored = self.cache.files()[0]
        data = self.root / 'outside'; data.write_bytes(b'one')
        stored.unlink(); stored.symlink_to(data)
        _, stats = self.prepare(network='none')
        self.assertEqual(stats['seeded_packages'], 0)

    def test_source_identity_versions_and_hashes_are_distinct(self):
        args = ['npm', 'one', '1', 'https://registry.npmjs.org/one.tgz', 'sha256', 'a' * 64, 'npm/one', 'lock']
        a = Artifact(*args)
        for index, value in ((2, '2'), (3, 'https://registry.npmjs.org/two.tgz'), (5, 'b' * 64)):
            changed = args.copy(); changed[index] = value
            self.assertNotEqual(a.key, Artifact(*changed).key)

    def test_unsupported_sources_and_secrets_do_not_reach_receipts(self):
        self.npm()
        lock = json.loads((self.tree / 'package-lock.json').read_text())
        package = next(iter(lock['packages'].values()))
        package['resolved'] = 'https://secret:password@registry.npmjs.org/package?token=private'
        self.write('package-lock.json', json.dumps(lock))
        self.write('requirements.txt', 'package>=1\n-e ./local\n')
        _, stats = self.prepare(('npm', 'pip'))
        self.assertEqual(stats['eligible_packages'], 0)
        self.assertTrue(stats['unsupported'])
        self.assertNotIn('password', json.dumps(stats))
        self.assertNotIn('token', json.dumps(stats))
        self.fetch.assert_not_called()
        for url in ('http://pypi.org/pkg', 'https://localhost/pkg', 'https://127.0.0.1/pkg',
                    'https://pypi.org:443/pkg', 'https://pypi.org/pkg?token=secret',
                    'https://user@pypi.org/pkg', 'https://pypi.org/pkg\nheader'):
            self.assertFalse(public_url(url))
        with self.assertRaises(ValueError):
            PublicRedirects().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/private')

    def python_lock(self, filename='demo-1.0-py3-none-any.whl', data=b'wheel'):
        url = 'https://files.pythonhosted.org/packages/' + filename
        self.downloads[url] = data
        checksum = hashlib.sha256(data).hexdigest()
        self.write('python/uv.lock', f'''version = 1
[[package]]
name = "demo"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
wheels = [{{ url = "{url}", hash = "sha256:{checksum}" }}]
''')
        return url, checksum

    def test_pip_uses_historical_hashes_and_pypi_metadata_is_reused(self):
        url, checksum = self.python_lock()
        self.write('python/uv.lock', '')
        self.write('requirements.txt', '-r python/pinned.txt\n')
        self.write('python/pinned.txt', f'demo==1.0 --hash=sha256:{checksum}\n')
        metadata_url = 'https://pypi.org/pypi/demo/1.0/json'
        self.downloads[metadata_url] = json.dumps({'urls': [{'url': url,
            'digests': {'sha256': checksum}, 'packagetype': 'bdist_wheel'},
            {'url': 'https://files.pythonhosted.org/not-authorized.whl',
             'digests': {'sha256': 'a' * 64}, 'packagetype': 'bdist_wheel'}]}).encode()
        local, first = self.prepare(('pip',))
        _, second = self.prepare(('pip',))
        self.assertEqual(self.fetch.call_count, 2)  # metadata and artifact, once each
        self.assertEqual(first['stored_packages'], 1)
        self.assertEqual(second['seeded_packages'], 1)
        self.assertEqual(len(list((local / 'pip/packages').iterdir())), 1)

    def test_uv_and_pylock_direct_archives_and_markers(self):
        url, checksum = self.python_lock()
        self.write('requirements.txt', f'demo @ {url}#sha256={checksum} ; python_version > "3"\n')
        self.write('pylock.toml', f'''lock-version = "1.0"
[[packages]]
name = "demo"
version = "1.0"
wheels = [{{ url = "{url}", hashes = {{ sha256 = "{checksum}" }} }}]
''')
        _, stats = self.prepare(('pip',))
        self.assertEqual(stats['eligible_packages'], 1)
        self.assertEqual(self.fetch.call_count, 1)

    def test_poetry_and_pipenv_share_only_archives_with_locked_hashes(self):
        url, checksum = self.python_lock()
        self.write('python/uv.lock', '')
        self.write('poetry.lock', f'''[[package]]
name = "demo"
version = "1.0"
files = [{{ file = "demo-1.0-py3-none-any.whl", hash = "sha256:{checksum}" }}]
''')
        self.write('Pipfile.lock', json.dumps({'_meta': {'sources': [{'url': 'https://pypi.org/simple'}]},
            'default': {'demo': {'version': '==1.0', 'hashes': ['sha256:' + checksum]}}}))
        self.downloads['https://pypi.org/pypi/demo/1.0/json'] = json.dumps({'urls': [
            {'url': url, 'digests': {'sha256': checksum}, 'packagetype': 'bdist_wheel'}]}).encode()
        _, stats = self.prepare(('pip',))
        self.assertEqual(stats['eligible_packages'], 1)
        self.assertEqual(self.fetch.call_count, 2)
        metadata = next((self.cache.root / 'metadata').iterdir())
        metadata.chmod(0o600); metadata.write_bytes(b'broken metadata')
        _, stats = self.prepare(('pip',))
        self.assertEqual(stats['seeded_packages'], 1)
        self.assertEqual(self.fetch.call_count, 3)  # repair metadata, keep verified wheel

    def test_native_pip_installs_hashed_wheel_into_independent_environments_offline(self):
        # Build a pure wheel fixture without executing a build backend.
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('demo.py', 'ANSWER = 42\n')
            archive.writestr('demo-1.0.dist-info/METADATA', 'Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n')
            archive.writestr('demo-1.0.dist-info/WHEEL', 'Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
            archive.writestr('demo-1.0.dist-info/RECORD', '')
        _, checksum = self.python_lock(data=stream.getvalue())
        local, _ = self.prepare(('pip',))
        venv = self.root / 'venv'
        created = subprocess.run([sys.executable, '-m', 'venv', str(venv)], capture_output=True, text=True, timeout=30)
        self.assertEqual(created.returncode, 0, created.stderr)
        requirement = self.write('locked.txt', f'demo==1.0 --hash=sha256:{checksum}\n')
        for name in ('first', 'second'):
            target = self.root / name
            result = subprocess.run([str(venv / 'bin/python'), '-m', 'pip', 'install', '--no-index',
                '--disable-pip-version-check', '--require-hashes', '--target', str(target), '-r', str(requirement)],
                env={**os.environ, 'PIP_FIND_LINKS': str(local / 'pip/packages'),
                     'PIP_CONFIG_FILE': os.devnull, 'PIP_CACHE_DIR': str(self.root / 'unused-cache')},
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((target / 'demo.py').read_text(), 'ANSWER = 42\n')
        self.assertNotEqual((self.root / 'first/demo.py').stat().st_ino,
                            (self.root / 'second/demo.py').stat().st_ino)
        (self.root / 'first/demo.py').write_text('mutated')
        self.assertEqual((self.root / 'second/demo.py').read_text(), 'ANSWER = 42\n')

    def go_module(self, name, version, requirements=''):
        module = f'module {name}\n\ngo 1.20\n{requirements}'.encode()
        files = {name + '@' + version + '/go.mod': module,
                 name + '@' + version + '/main.go': b'package example\nconst Answer = 42\n'}
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            for filename, data in files.items():
                archive.writestr(filename, data)
        escaped = ''.join('!' + c.lower() if c.isupper() else c for c in name)
        base = 'https://proxy.golang.org/' + escaped + '/@v/' + version
        self.downloads[base + '.mod'] = module
        self.downloads[base + '.zip'] = stream.getvalue()
        return f'{name} {version}/go.mod {hash1([("go.mod", module)])}\n{name} {version} {hash1(files.items())}\n'

    def go_project(self):
        self.write('go.mod', 'module example.test/root\n\ngo 1.20\nrequire example.test/Upper v1.0.0\n')
        sums = self.go_module('example.test/Upper', 'v1.0.0', 'require example.test/transitive v1.2.0\n')
        sums += self.go_module('example.test/transitive', 'v1.2.0')
        sums += self.go_module('example.test/unrelated', 'v1.0.0')
        self.write('go.sum', sums)

    def test_go_transitive_graph_hashes_and_unrelated_sums(self):
        self.go_project()
        local, first = self.prepare(('go',))
        _, second = self.prepare(('go',), network='none')
        self.assertEqual(first['eligible_packages'], 4)
        self.assertEqual(second['seeded_packages'], 4)
        self.assertEqual(self.fetch.call_count, 4)
        self.assertNotIn('example.test/unrelated', [a['name'] for a in second['artifacts']])
        self.assertTrue((local / 'go/cache/download/example.test/!upper/@v/v1.0.0.ziphash').is_file())

    @unittest.skipUnless(shutil.which('go'), 'Go required')
    def test_native_go_downloads_transitive_dependencies_offline(self):
        self.go_project()
        local, _ = self.prepare(('go',))
        result = subprocess.run(['go', 'mod', 'download', 'all'], cwd=self.tree,
            env={**os.environ, 'GOMODCACHE': str(local / 'go'), 'GOCACHE': str(self.root / 'build'),
                 'GOPROXY': 'off', 'GOSUMDB': 'off', 'GOTOOLCHAIN': 'local', 'GOWORK': 'off'},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((local / 'go/example.test/!upper@v1.0.0/main.go').is_file())
        self.assertTrue((local / 'go/example.test/transitive@v1.2.0/main.go').is_file())

    def cargo_crate(self, malicious=False):
        files = {'demo-1.0.0/Cargo.toml': b'[package]\nname="demo"\nversion="1.0.0"\nedition="2021"\n',
                 'demo-1.0.0/src/lib.rs': b'pub const ANSWER: i32 = 42;\n'}
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for name, data in files.items():
                entry = tarfile.TarInfo(name); entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
            if malicious:
                entry = tarfile.TarInfo('demo-1.0.0/link'); entry.type = tarfile.SYMTYPE
                entry.linkname = '/etc/passwd'; archive.addfile(entry)
        data = stream.getvalue()
        url = 'https://static.crates.io/crates/demo/demo-1.0.0.crate'
        self.downloads[url] = data
        checksum = hashlib.sha256(data).hexdigest()
        self.write('Cargo.lock', f'''version = 3
[[package]]
name = "root"
version = "1.0.0"
dependencies = ["demo"]
[[package]]
name = "demo"
version = "1.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "{checksum}"
''')
        self.write('Cargo.toml', '[package]\nname="root"\nversion="1.0.0"\nedition="2021"\n[dependencies]\ndemo="=1.0.0"\n')
        self.write('src/main.rs', 'fn main() { assert_eq!(demo::ANSWER, 42); }\n')
        return checksum

    def test_cargo_archives_only_are_persisted_and_unsafe_archives_fall_back(self):
        checksum = self.cargo_crate()
        local, stats = self.prepare(('cargo',))
        self.assertEqual(stats['stored_packages'], 1)
        self.assertTrue((local / 'cargo/vendor/demo-1.0.0/.cargo-checksum.json').is_file())
        self.assertEqual(len(self.cache.files()), 1)
        self.assertFalse((self.cache.root / 'vendor').exists())
        self.cargo_crate(malicious=True)
        local, stats = self.prepare(('cargo',))
        self.assertFalse((local / 'cargo/vendor-config.toml').exists())
        self.assertTrue(stats['unsupported'])

    @unittest.skipUnless(shutil.which('cargo'), 'Cargo required')
    def test_native_cargo_installs_locked_crate_without_registry_or_network(self):
        self.cargo_crate()
        local, _ = self.prepare(('cargo',))
        config = local / 'cargo/vendor-config.toml'
        config.write_text(config.read_text().replace('/mineval-dependency-cache', str(local)))
        self.assertFalse((local / 'cargo/config.toml').exists())
        result = subprocess.run(['cargo', '--config', str(config), 'run', '--offline', '--locked', '--quiet'], cwd=self.tree,
            env={**os.environ, 'CARGO_HOME': str(local / 'cargo'), 'CARGO_TARGET_DIR': str(self.root / 'build')},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.cache.root / 'target').exists())

    def test_frozen_approved_manifest_is_used_instead_of_base_or_live_tree(self):
        self.npm('approved')
        snapshot = self.root / 'snapshot.tar.gz'; pack(self.tree, snapshot)
        self.npm('live-agent-change')
        cache = DependencyCache(self.cache.root, self.tree, snapshot=snapshot)
        local, stats = cache.prepare(('npm',), network='bridge')
        self.addCleanup(shutil.rmtree, local, True)
        self.assertEqual(stats['artifacts'][0]['name'], 'approved')
        self.assertFalse(list(cache.root.glob('.snapshot-*')))

    def sandbox(self, labels):
        sandbox = DockerSandbox(); sandbox.dependency_cache = self.cache
        cli = Mock(); sandbox._cli = cli
        cli.json.side_effect = lambda *args: READY.copy() if args[0] == 'info' else [
            {'Id': IMAGE_ID, 'Config': {'Labels': labels}}]
        cli.call.return_value = (CONTAINER_ID + '\n', '', False)
        workspace = self.root / 'workspace'; workspace.mkdir()
        return sandbox, cli, SandboxSpec('image', ('true',), workspace=workspace)

    def test_sandbox_opt_in_mounts_only_private_task_data_and_no_agent_import(self):
        self.python_lock()
        sandbox, cli, spec = self.sandbox({'io.mineval.dependency-cache': 'pip,go,cargo'})
        with sandbox.container(spec) as container:
            args = cli.call.call_args_list[0].args
            mount = next(a for a in args if isinstance(a, str) and 'dst=/mineval-dependency-cache,' in a)
            local = Path(mount.split('src=', 1)[1].split(',', 1)[0])
            self.assertNotEqual(local, self.cache.root)
            self.assertEqual(container.cache_stats['missed_packages'], 1)
            self.fetch.assert_not_called()  # spec.network is none
            self.assertIn('--env=GOMODCACHE=/mineval-dependency-cache/go', args)
            self.assertIn('--env=PIP_FIND_LINKS=/mineval-dependency-cache/pip/packages', args)
            (local / 'secret').write_text('must not persist')
        self.assertFalse(local.exists())
        self.assertFalse((self.cache.root / 'secret').exists())

    def test_disabled_image_preserves_existing_behavior(self):
        self.npm()
        sandbox, cli, spec = self.sandbox({})
        with sandbox.container(spec):
            args = cli.call.call_args_list[0].args
            self.assertFalse(any('mineval-dependency-cache' in a for a in args))
        self.fetch.assert_not_called()

    def test_failed_removal_quarantines_private_files(self):
        sandbox, cli, spec = self.sandbox({'io.mineval.dependency-cache': 'pip'})
        def call(*args):
            if args[0] == 'rm':
                raise RuntimeError('removal failed')
            return CONTAINER_ID + '\n', '', False
        cli.call.side_effect = call
        with self.assertRaisesRegex(RuntimeError, 'removal failed'):
            with sandbox.container(spec):
                local = next(self.cache.root.glob('.run-*'))
                (local / 'agent-output').write_text('quarantined')
        self.assertTrue(local.exists())
        self.cache.prune(0)
        self.assertTrue(local.exists())
        self.assertEqual(self.cache.status()['packages'], 0)


if __name__ == '__main__':
    unittest.main()
