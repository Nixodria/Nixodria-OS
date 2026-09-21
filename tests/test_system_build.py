"""Host-side safety checks for preserving the writable Rust disk."""

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
import build_system
from qemu_guest import command


class RuntimeImageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.image = root / 'base.qcow2'
        self.runtime = root / 'runtime.qcow2'
        self.image.write_bytes(b'new system template')
        self.image_patch = patch.object(build_system, 'IMAGE', self.image)
        self.runtime_patch = patch.object(build_system, 'RUNTIME', self.runtime)
        self.image_patch.start()
        self.runtime_patch.start()
        self.addCleanup(self.image_patch.stop)
        self.addCleanup(self.runtime_patch.stop)

    def test_existing_runtime_never_overwritten(self):
        self.runtime.write_bytes(b'user projects')
        build_system.prepare_runtime()
        self.assertEqual(self.runtime.read_bytes(), b'user projects')

    def test_symlink_runtime_refused(self):
        self.runtime.symlink_to(self.image)
        with self.assertRaises(RuntimeError):
            build_system.prepare_runtime()
        self.assertEqual(self.image.read_bytes(), b'new system template')

    def test_new_runtime_private_and_complete(self):
        build_system.prepare_runtime()
        self.assertEqual(self.runtime.read_bytes(), self.image.read_bytes())
        self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o600)

    def test_copy_failure_does_not_publish_partial_runtime(self):
        def fail(source, destination):
            destination.write(b'partial')
            raise OSError('simulated full disk')
        with patch.object(build_system.shutil, 'copyfileobj', side_effect=fail):
            with self.assertRaises(OSError):
                build_system.prepare_runtime()
        self.assertFalse(self.runtime.exists())
        self.assertEqual(sorted(path.name for path in self.image.parent.iterdir()), ['base.qcow2'])

    def test_interactive_ctrl_c_reaches_guest(self):
        args = command(self.runtime, network=False)
        self.assertIn('stdio,id=console,signal=off', args)
        self.assertIn('none', args)
        self.assertNotIn('user,id=net0', args)


if __name__ == '__main__':
    unittest.main()
