import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class InstallerTests(unittest.TestCase):
    def test_install_upgrade_uninstall_and_verification_failure(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/install.sh'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); tools = root / 'tools'; tools.mkdir()
            binary = root / 'binary'; binary.write_text('#!/bin/sh\necho mineval\n')
            curl = tools / 'curl'
            curl.write_text('''#!/bin/sh
for arg do
 case "$arg" in https:*) url=$arg;; esac
 last=$arg
done
case "$url" in *.sha256) printf '%s  mineval-v0.1.0-linux-x86_64\\n' "$TEST_HASH" > "$last";; *) cp "$TEST_BINARY" "$last";; esac
''')
            gh = tools / 'gh'; gh.write_text('#!/bin/sh\nexit "${TEST_VERIFY_FAIL:-0}"\n')
            for tool in (curl, gh): tool.chmod(0o755)
            env = os.environ | {'PATH': str(tools) + ':' + os.environ['PATH'],
                'MINEVAL_BIN_DIR': str(root / 'bin'), 'TEST_BINARY': str(binary),
                'TEST_HASH': hashlib.sha256(binary.read_bytes()).hexdigest()}
            def run(version, fail=False):
                return subprocess.run(['sh', str(script), version], env=env | {'TEST_VERIFY_FAIL': '1' if fail else '0'}, capture_output=True)
            self.assertNotEqual(run('v0.1.0', True).returncode, 0)
            self.assertFalse((root / 'bin/mineval').exists())
            self.assertEqual(run('v0.1.0').returncode, 0)
            self.assertEqual(run('v0.1.0').returncode, 0)
            self.assertEqual((root / 'bin/mineval').read_bytes(), binary.read_bytes())
            self.assertEqual(run('uninstall').returncode, 0)
            self.assertFalse((root / 'bin/mineval').exists())
