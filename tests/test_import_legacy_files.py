#!/usr/bin/env python3
"""Check legacy guest-import bundles without touching a real runtime disk."""

import hashlib
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import import_legacy_files as importer
from test_export_legacy_files import image, snapshot


class ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="nixodria-import-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "legacy.img"
        self.archive = self.root / "legacy.tar"
        self.files = ((b"HELLO.BAS", b'10 PRINT "HELLO"\r\n20 END\r\n'),
                      (b"DATA.TXT", b"\0\xff"), (b"-DASH.BAS", b"source"))
        self.original = image(snapshot(3, self.files))
        self.source.write_bytes(self.original)

    def test_bundle_contains_only_regular_private_byte_exact_saved_files(self) -> None:
        bundle = importer.build_bundle(self.source, self.archive)
        self.assertEqual(bundle.archive_digest, hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.assertEqual(bundle.image_digest, hashlib.sha256(self.original).hexdigest())
        self.assertEqual(bundle.destination, "/root/workspace/legacy-" + bundle.image_digest[:12])
        self.assertEqual(stat.S_IMODE(self.archive.stat().st_mode), 0o600)
        with tarfile.open(self.archive) as archive:
            self.assertEqual(archive.getnames(), [name.decode() for name, _ in self.files])
            for member, (name, content) in zip(archive.getmembers(), self.files):
                self.assertEqual(member.name, name.decode())
                self.assertTrue(member.isfile())
                self.assertEqual(member.mode, 0o600)
                self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
                self.assertEqual(archive.extractfile(member).read(), content)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_bundle_is_deterministic(self) -> None:
        first = importer.build_bundle(self.source, self.archive)
        second = importer.build_bundle(self.source, self.root / "second.tar")
        self.assertEqual(first.archive_digest, second.archive_digest)
        self.assertEqual(first.destination, second.destination)

    def test_existing_archive_is_not_overwritten(self) -> None:
        self.archive.write_bytes(b"keep me")
        with self.assertRaises(FileExistsError):
            importer.build_bundle(self.source, self.archive)
        self.assertEqual(self.archive.read_bytes(), b"keep me")

    def test_blank_source_has_no_archive_members(self) -> None:
        self.source.write_bytes(image())
        bundle = importer.build_bundle(self.source, self.archive)
        self.assertEqual(bundle.files, ())
        with tarfile.open(self.archive) as archive:
            self.assertEqual(archive.getmembers(), [])

    def test_http_server_serves_only_the_exact_archive_path(self) -> None:
        bundle = importer.build_bundle(self.source, self.archive)
        with importer.serve_bundle(bundle) as guest_url:
            url = guest_url.replace("10.0.2.2", "127.0.0.1")
            with urllib.request.urlopen(url, timeout=3) as response:
                self.assertEqual(response.read(), self.archive.read_bytes())
            base = url.rsplit("/", 1)[0]
            for path in ("/", "/legacy.tar", "/legacy.img", "/../legacy.img"):
                with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(base + path, timeout=3)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()

    def test_http_server_refuses_changed_archive(self) -> None:
        bundle = importer.build_bundle(self.source, self.archive)
        self.archive.write_bytes(b"changed")
        with self.assertRaises(importer.ExportError):
            with importer.serve_bundle(bundle):
                self.fail("changed archive served")

    def test_shell_script_quotes_url_and_verifies_archive_and_each_file(self) -> None:
        bundle = importer.build_bundle(self.source, self.archive)
        script = importer.import_script(bundle, "http://example.invalid/a'$(touch bad)")
        self.assertIn("mv -nT --", script)
        self.assertIn('if [ -e "$destination" ] || [ -L "$destination" ]', script)
        self.assertIn(bundle.archive_digest, script)
        self.assertIn("'http://example.invalid/a'\"'\"'$(touch bad)'", script)
        for name, content in self.files:
            self.assertIn(f"{hashlib.sha256(content).hexdigest()}  ./{name.decode()}", script)

    def test_runtime_must_be_regular_qcow2_without_symlinks(self) -> None:
        runtime = self.root / "runtime.qcow2"
        runtime.write_bytes(b"QFI\xfb" + b"\0" * 100)
        self.assertEqual(importer.checked_runtime(runtime), runtime)
        link = self.root / "link.qcow2"
        link.symlink_to(runtime)
        with self.assertRaises(OSError):
            importer.checked_runtime(link)
        runtime.write_bytes(b"not qcow2")
        with self.assertRaises(importer.ExportError):
            importer.checked_runtime(runtime)
        comma = self.root / "runtime,option.qcow2"
        comma.write_bytes(b"QFI\xfb" + b"\0" * 100)
        with self.assertRaises(importer.ExportError):
            importer.checked_runtime(comma)


if __name__ == "__main__":
    unittest.main()
