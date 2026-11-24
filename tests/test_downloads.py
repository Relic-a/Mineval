"""Maintenance and safe reads for the legacy npm store."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from mineval.addons.cache.dependency_cache import NpmCache


class LegacyNpmCacheTests(unittest.TestCase):
    def test_legacy_status_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = NpmCache(tmp)
            payload = cache.root / cache.relative(hashlib.sha512(b'payload').hexdigest())
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b'payload')
            self.assertEqual(cache.status()['packages'], 1)
            self.assertEqual(cache.prune(0)['removed_packages'], 1)
            self.assertFalse(payload.exists())

    def test_payload_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            digest = hashlib.sha512(b'payload').hexdigest()
            payload = root / NpmCache.relative(digest)
            payload.parent.mkdir(parents=True)
            source = root / 'other'
            source.write_bytes(b'payload')
            payload.symlink_to(source)
            self.assertIsNone(NpmCache.payload(root, digest))
