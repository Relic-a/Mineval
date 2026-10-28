import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import subprocess
import tarfile
import io
import unittest
from unittest.mock import Mock

from mineval.downloads import NpmCache
from mineval.sandbox import DockerSandbox, SandboxSpec
from test_sandbox import READY, IMAGE_ID, CONTAINER_ID


class DownloadCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock = self.root / 'package-lock.json'
        self.data = b'public package tarball fixture'
        self.digest = hashlib.sha512(self.data).hexdigest()
        self.package = {'resolved': 'https://registry.npmjs.org/example/-/example-1.tgz',
                        'integrity': 'sha512-' + base64.b64encode(bytes.fromhex(self.digest)).decode()}
        self.lock.write_text(json.dumps({'lockfileVersion': 3, 'packages': {'node_modules/example': self.package}}))
        self.cache = NpmCache(self.root / 'cache', self.lock)

    def put(self, root, digest=None, data=None):
        path = root / self.cache.relative(digest or self.digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.data if data is None else data)
        return path

    def test_only_base_lockfile_verified_public_payloads_are_collected(self):
        local, stats = self.cache.prepare()
        self.put(local)
        self.put(local, 'b' * 128, b'private data from a different source')
        (local / 'credentials.json').write_text('must not persist')
        (local / 'index-v5').mkdir()
        (local / 'index-v5/private').write_text('private registry metadata')
        self.cache.collect(local, stats)
        self.assertEqual(self.cache.status()['packages'], 1)
        self.assertEqual(stats['stored_packages'], 1)
        self.assertFalse((self.cache.root / 'credentials.json').exists())
        self.assertFalse((self.cache.root / 'index-v5').exists())

    def test_private_urls_query_tokens_and_weak_or_invalid_hashes_are_not_allowed(self):
        packages = {}
        for i, change in enumerate(({'resolved': 'https://private.example/pkg.tgz'},
                                    {'resolved': 'https://registry.npmjs.org/pkg?token=secret'},
                                    {'resolved': 'https://secret@registry.npmjs.org/pkg'},
                                    {'integrity': 'sha1-abcd'}, {'integrity': 'sha512-!!'},
                                    {'resolved': 'https://['}, {'integrity': 3})):
            packages[str(i)] = self.package | change
        self.lock.write_text(json.dumps({'packages': packages}))
        self.assertEqual(self.cache.allowed(), set())

    def test_corrupt_payload_and_symlink_directory_or_file_are_rejected(self):
        local, stats = self.cache.prepare()
        path = self.put(local, data=b'corrupted')
        self.cache.collect(local, stats)
        self.assertEqual(self.cache.status()['packages'], 0)
        outside = self.root / 'outside'
        outside.write_bytes(self.data)
        path.unlink(); path.symlink_to(outside)
        self.cache.collect(local, stats)
        self.assertEqual(self.cache.status()['packages'], 0)
        path.unlink(); path.parent.rmdir()
        folder = self.root / 'outside-dir'; folder.mkdir()
        (folder / path.name).write_bytes(self.data)
        path.parent.symlink_to(folder, target_is_directory=True)
        self.cache.collect(local, stats)
        self.assertEqual(self.cache.status()['packages'], 0)

    def test_writable_copies_cannot_poison_persistent_or_later_cache(self):
        stored = self.put(self.cache.root)
        first, a = self.cache.prepare()
        self.assertEqual(a['seeded_packages'], 1)
        copied = first / self.cache.relative(self.digest)
        self.assertNotEqual(copied.stat().st_ino, stored.stat().st_ino)
        copied.write_bytes(b'poison')
        self.cache.collect(first, a)
        second, b = self.cache.prepare()
        self.assertEqual((second / self.cache.relative(self.digest)).read_bytes(), self.data)
        self.assertEqual(b['seeded_packages'], 1)

    def test_eviction_removes_oldest_payloads_and_respects_budget(self):
        first = self.put(self.cache.root)
        other = self.put(self.cache.root, 'b' * 128, b'other')
        os.utime(first, (1, 1)); os.utime(other, (2, 2))
        result = self.cache.prune(max_bytes=len(b'other'))
        self.assertFalse(first.exists())
        self.assertTrue(other.exists())
        self.assertEqual(result['bytes'], len(b'other'))

    def test_corrupt_persistent_entry_is_not_seeded(self):
        self.put(self.cache.root, data=b'corrupt')
        local, stats = self.cache.prepare()
        self.assertEqual(stats['seeded_packages'], 0)
        self.assertFalse((local / self.cache.relative(self.digest)).exists())

    def test_only_intersection_is_seeded_for_another_task(self):
        other_data = b'other task dependency'
        other_digest = hashlib.sha512(other_data).hexdigest()
        self.put(self.cache.root)
        self.put(self.cache.root, other_digest, other_data)
        local, stats = self.cache.prepare()
        self.assertTrue((local / self.cache.relative(self.digest)).exists())
        self.assertFalse((local / self.cache.relative(other_digest)).exists())
        self.assertEqual(stats['eligible_packages'], 1)
        self.assertEqual(stats['missed_packages'], 0)

    def test_manifest_is_snapshotted_and_reports_coverage(self):
        local, stats = self.cache.prepare()
        self.assertEqual(stats['missed_packages'], 1)
        self.assertEqual(stats['artifacts'][0]['sha512'], self.digest)
        self.lock.write_text(json.dumps({'packages': {}}))
        self.put(local)
        self.cache.collect(local, stats)
        self.assertEqual(stats['stored_packages'], 1)
        self.lock.write_text(json.dumps({'packages': {'node_modules/private': {
            'resolved': 'https://secret.example/package?token=secret', 'integrity': 'sha1-abc'}}}))
        _, stats = self.cache.prepare()
        self.assertEqual(stats['unsupported_entries'], 1)
        self.assertNotIn('secret', json.dumps(stats))

    @unittest.skipUnless(shutil.which('npm'), 'npm is required')
    def test_seeded_tarball_is_usable_by_npm_offline(self):
        archive = self.root / 'package.tgz'
        package_json = json.dumps({'name': 'cache-fixture', 'version': '1.0.0'}).encode()
        with tarfile.open(archive, 'w:gz') as tar:
            entry = tarfile.TarInfo('package/package.json')
            entry.size = len(package_json)
            tar.addfile(entry, io.BytesIO(package_json))
        data = archive.read_bytes()
        digest = hashlib.sha512(data).hexdigest()
        integrity = 'sha512-' + base64.b64encode(bytes.fromhex(digest)).decode()
        package = {'version': '1.0.0', 'resolved': 'https://registry.npmjs.org/cache-fixture/-/cache-fixture-1.0.0.tgz',
                   'integrity': integrity}
        self.lock.write_text(json.dumps({'name': 'fixture', 'lockfileVersion': 3,
            'packages': {'': {'name': 'fixture', 'dependencies': {'cache-fixture': '1.0.0'}},
                         'node_modules/cache-fixture': package}}))
        self.put(self.cache.root, digest, data)
        local, stats = self.cache.prepare()
        project = self.root / 'project'; project.mkdir()
        (project / 'package.json').write_text(json.dumps({'name': 'fixture', 'version': '1.0.0',
                                                          'dependencies': {'cache-fixture': '1.0.0'}}))
        shutil.copyfile(self.lock, project / 'package-lock.json')
        result = subprocess.run(['npm', 'ci', '--offline', '--ignore-scripts', '--no-audit', '--no-fund',
                                 '--cache', str(local)], cwd=project, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((project / 'node_modules/cache-fixture/package.json').exists())
        self.assertEqual(stats['seeded_packages'], 1)

    def sandbox(self):
        sandbox = DockerSandbox()
        sandbox.npm_cache = self.cache
        cli = Mock()
        sandbox._cli = cli
        cli.json.side_effect = lambda *args: READY.copy() if args[0] == 'info' else [
            {'Id': IMAGE_ID, 'Config': {'Labels': {'io.mineval.npm-cache': 'v1'}}}]
        cli.call.return_value = (CONTAINER_ID + '\n', '', False)
        workspace = self.root / 'workspace'; workspace.mkdir()
        return sandbox, cli, SandboxSpec('image', ('true',), workspace=workspace)

    def test_sandbox_mounts_only_private_copy_and_collects_after_removal(self):
        self.put(self.cache.root)
        sandbox, cli, spec = self.sandbox()
        with sandbox.container(spec) as container:
            call = cli.call.call_args_list[0].args
            mount = next(arg for arg in call if isinstance(arg, str) and 'dst=/mineval-npm-cache,' in arg)
            local = Path(mount.split('src=', 1)[1].split(',', 1)[0])
            self.assertNotEqual(local, self.cache.root)
            self.assertIn('--env=NPM_CONFIG_CACHE=/mineval-npm-cache', call)
            self.assertEqual(container.cache_stats['seeded_packages'], 1)
            self.assertEqual(list(spec.workspace.iterdir()), [])
        self.assertFalse(local.exists())
        self.assertEqual(cli.call.call_args.args[:3], ('rm', '--force', '--volumes'))

    def test_failed_container_removal_quarantines_cache_without_importing(self):
        sandbox, cli, spec = self.sandbox()
        def call(*args):
            if args[0] == 'rm':
                raise RuntimeError('removal failed')
            return CONTAINER_ID + '\n', '', False
        cli.call.side_effect = call
        with self.assertRaisesRegex(RuntimeError, 'removal failed'):
            with sandbox.container(spec):
                local = next(self.cache.root.glob('.run-*'))
                self.put(local)
        self.assertTrue(local.exists())
        self.assertEqual(self.cache.status()['packages'], 0)


if __name__ == '__main__':
    unittest.main()
