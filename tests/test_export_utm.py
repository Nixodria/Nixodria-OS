"""Exercise standalone UTM exports and preservation of existing VM data."""

import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import export_utm

QEMU_IMG = os.environ.get("QEMU_IMG", "qemu-img")


@unittest.skipUnless(shutil.which(QEMU_IMG), "qemu-img is required")
class ExportUTMTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.image = self.root / "source.qcow2"
        self.output = self.root / "Nixodria.utm"
        subprocess.run([QEMU_IMG, "create", "-f", "qcow2", str(self.image), "16M"],
                       check=True, capture_output=True)
        self.original = self.image.read_bytes()

    def test_export_is_standalone_and_source_is_unchanged(self):
        export_utm.export(self.image, self.output)
        with (self.output / "config.plist").open("rb") as stream:
            config = plistlib.load(stream)
        disk = self.output / "Data" / config["Drive"][0]["ImageName"]
        info = json.loads(subprocess.check_output(
            [QEMU_IMG, "info", "--output=json", str(disk)]))
        self.assertEqual(info["format"], "qcow2")
        self.assertEqual(info["virtual-size"], 16 * 1024 * 1024)
        self.assertNotIn("backing-filename", info)
        self.assertEqual(disk.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.image.read_bytes(), self.original)
        # Relocation must not depend on checkout-absolute disk paths.
        relocated = self.root / "Moved.utm"
        self.output.rename(relocated)
        subprocess.run([QEMU_IMG, "check", str(relocated / "Data" / disk.name)],
                       check=True, capture_output=True)

    def test_existing_bundle_and_saved_work_are_preserved(self):
        self.output.mkdir()
        saved = self.output / "saved-work"
        saved.write_bytes(b"user projects")
        with self.assertRaises((OSError, RuntimeError)):
            export_utm.export(self.image, self.output)
        self.assertEqual(saved.read_bytes(), b"user projects")
        self.assertEqual(list(self.output.iterdir()), [saved])

    def test_publication_race_preserves_saved_work_and_removes_partial_bundle(self):
        publish = export_utm.publish_exclusively
        saved = self.output / "saved-work"

        def create_destination_before_publication(temporary, output):
            output.mkdir()
            saved.write_bytes(b"user projects")
            publish(temporary, output)

        with patch.object(export_utm, "publish_exclusively",
                          side_effect=create_destination_before_publication):
            with self.assertRaisesRegex(export_utm.ExportError, "destination already exists"):
                export_utm.export(self.image, self.output)
        self.assertEqual(saved.read_bytes(), b"user projects")
        self.assertEqual(list(self.output.iterdir()), [saved])
        self.assertEqual(set(self.root.iterdir()), {self.image, self.output})
        self.assertEqual(self.image.read_bytes(), self.original)

    def test_backed_image_is_flattened_without_changing_guest_contents(self):
        raw = self.root / "contents.raw"
        with raw.open("wb") as stream:
            stream.write(b"saved workspace data\n" * 128)
            stream.truncate(16 * 1024 * 1024)
        subprocess.run([QEMU_IMG, "convert", "-f", "raw", "-O", "qcow2",
                        str(raw), str(self.image)], check=True, capture_output=True)
        overlay = self.root / "overlay.qcow2"
        subprocess.run([QEMU_IMG, "create", "-f", "qcow2", "-F", "qcow2",
                        "-b", str(self.image), str(overlay)], check=True, capture_output=True)
        export_utm.export(overlay, self.output)
        disk = self.output / "Data" / "nixodria.qcow2"
        info = json.loads(subprocess.check_output(
            [QEMU_IMG, "info", "--output=json", str(disk)]))
        self.assertNotIn("backing-filename", info)
        subprocess.run([QEMU_IMG, "compare", "-f", "qcow2", "-F", "qcow2",
                        str(overlay), str(disk)], check=True, capture_output=True)

    def test_empty_destination_is_not_replaced(self):
        self.output.mkdir()
        inode = self.output.stat().st_ino
        with self.assertRaises((OSError, RuntimeError)):
            export_utm.export(self.image, self.output)
        self.assertEqual(self.output.stat().st_ino, inode)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_dangling_destination_symlink_is_not_replaced(self):
        self.output.symlink_to(self.root / "absent")
        with self.assertRaises((OSError, RuntimeError)):
            export_utm.export(self.image, self.output)
        self.assertTrue(self.output.is_symlink())
        self.assertFalse((self.root / "absent").exists())

    def test_failed_conversion_does_not_publish_or_leave_partial_bundle(self):
        run = subprocess.run

        def fail_conversion(args, *positional, **keywords):
            if "convert" in args:
                Path(args[-1]).write_bytes(b"partial copy")
                raise subprocess.CalledProcessError(1, args)
            return run(args, *positional, **keywords)

        with patch.object(export_utm.subprocess, "run", side_effect=fail_conversion):
            with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                export_utm.export(self.image, self.output)
        self.assertEqual(list(self.root.iterdir()), [self.image])
        self.assertEqual(self.image.read_bytes(), self.original)

    def test_non_qcow2_source_is_rejected(self):
        self.image.write_bytes(b"not a qcow2 disk")
        with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
            export_utm.export(self.image, self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
