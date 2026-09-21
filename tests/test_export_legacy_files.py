#!/usr/bin/env python3
"""Legacy migration regressions; fixtures never read the user's runtime image."""

import binascii
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import export_legacy_files as exporter


def crc(data: bytes) -> bytes:
    return binascii.crc_hqx(data, 0xFFFF).to_bytes(2, "little")


def snapshot(generation: int, files: tuple[tuple[bytes, bytes], ...]) -> bytes:
    data = bytearray(exporter.SNAPSHOT_SIZE)
    data[:4] = b"NIX3"
    data[4:6] = generation.to_bytes(2, "little")
    data[6] = len(files)
    for index, (name, content) in enumerate(files):
        entry = 8 + 18 * index
        data[entry : entry + len(name)] = name
        data[entry + 14 : entry + 16] = len(content).to_bytes(2, "little")
        data[entry + 16 : entry + 18] = crc(content)
        start = 512 + 2048 * index
        data[start : start + len(content)] = content
    data[152:154] = crc(data[:152])
    return bytes(data)


def legacy(generation: int, content: bytes) -> bytes:
    data = bytearray(exporter.LEGACY_SLOT_SIZE)
    data[:4] = b"NIX2"
    data[4:6] = generation.to_bytes(2, "little")
    data[6:8] = len(content).to_bytes(2, "little")
    data[8:10] = crc(content)
    data[10:12] = crc(data[:10])
    data[512 : 512 + len(content)] = content
    return bytes(data)


def image(first: bytes = b"", second: bytes = b"", size: int = exporter.IMAGE_SIZE) -> bytes:
    data = bytearray(size)
    data[510:512] = b"\x55\xaa"
    offsets = exporter.SNAPSHOT_OFFSETS if size == exporter.IMAGE_SIZE else (size - 5120, size - 2560)
    for offset, record in zip(offsets, (first, second)):
        data[offset : offset + len(record)] = record
    return bytes(data)


class RecoveryTests(unittest.TestCase):
    def test_crc_known_vector(self) -> None:
        self.assertEqual(exporter.checksum16(b"123456789"), 0x29B1)

    def test_selects_newer_and_preserves_all_bytes(self) -> None:
        files = ((b"HELLO.BAS", b'10 PRINT "HELLO"\r\n20 END\r\n'),
                 (b"RAW.TXT", b"\0\xff\r\n\n"), (b"EMPTY.TXT", b""))
        result = exporter.recover_files(image(snapshot(1, ()), snapshot(2, files)))
        self.assertEqual(result, exporter.Snapshot(1, 2, "NIX3", files))

    def test_maximum_directory_and_payload_sizes(self) -> None:
        files = tuple((f"FILE{i:04}.BAS".encode(), bytes([i]) * 2047) for i in range(8))
        result = exporter.recover_files(image(snapshot(9, files)))
        self.assertEqual(result.files, files)

    def test_generation_wrap_tie_and_half_range_match_bios(self) -> None:
        for first, second, winner in ((65535, 0, 1), (0, 65535, 0), (7, 7, 0),
                                      (32768, 0, 0), (0, 32768, 0), (8, 7, 0)):
            with self.subTest(first=first, second=second):
                result = exporter.recover_files(image(snapshot(first, ()), snapshot(second, ())))
                self.assertIsNotNone(result)
                self.assertEqual(result.slot, winner)

    def test_falls_back_when_newest_header_or_payload_corrupted(self) -> None:
        valid = snapshot(7, ((b"SAVE.BAS", b"original"),))
        for offset in (4, 152, 512):
            damaged = bytearray(snapshot(8, ((b"SAVE.BAS", b"changed"),)))
            damaged[offset] ^= 1
            result = exporter.recover_files(image(valid, damaged))
            self.assertEqual(result.files, ((b"SAVE.BAS", b"original"),))
            self.assertEqual(result.slot, 0)

    def test_all_files_in_a_snapshot_must_validate(self) -> None:
        damaged = bytearray(snapshot(8, ((b"ONE.BAS", b"one"), (b"TWO.BAS", b"two"))))
        damaged[512 + 2048] ^= 1
        with self.assertRaisesRegex(exporter.ExportError, "neither"):
            exporter.recover_files(image(damaged))

    def test_rejects_invalid_names_and_duplicate_entries(self) -> None:
        for name in (b".", b"..", b"../X", b"X/Y", b"lower.bas", b"", b"ABCDEFGHIJKLM", b"A\0B"):
            with self.subTest(name=name), self.assertRaises(exporter.ExportError):
                exporter.recover_files(image(snapshot(1, ((name, b"content"),))))
        with self.assertRaises(exporter.ExportError):
            exporter.recover_files(image(snapshot(1, ((b"A.BAS", b"a"), (b"A.BAS", b"b")))))

    def test_rejects_reserved_fields_count_and_length_even_with_correct_crc(self) -> None:
        for offset, replacement in ((7, b"\x01"), (6, b"\x09"), (21, b"\x01"), (22, b"\0\x08")):
            bad = bytearray(snapshot(1, ((b"A.BAS", b"a"),)))
            bad[offset : offset + len(replacement)] = replacement
            bad[152:154] = crc(bad[:152])
            with self.subTest(offset=offset), self.assertRaises(exporter.ExportError):
                exporter.recover_files(image(bad))

    def test_old_nix2_layouts_and_records_migrated_into_floppy_slots(self) -> None:
        for size in (*exporter.LEGACY_IMAGE_SIZES, exporter.IMAGE_SIZE):
            with self.subTest(size=size):
                result = exporter.recover_files(image(legacy(65535, b"old"), legacy(0, b"new\r\n"), size))
                self.assertEqual(result, exporter.Snapshot(1, 0, "NIX2", ((b"UNTITLED.TXT", b"new\r\n"),)))

    def test_mixed_nix2_and_nix3_generations(self) -> None:
        result = exporter.recover_files(image(legacy(2, b"old"), snapshot(3, ((b"NEW.RS", b"new"),))))
        self.assertEqual(result.files, ((b"NEW.RS", b"new"),))
        result = exporter.recover_files(image(snapshot(2, ()), legacy(3, b"new")))
        self.assertEqual(result.version, "NIX2")

    def test_rejects_corrupt_nix2_records_and_out_of_range_length(self) -> None:
        for offset in (4, 10, 512):
            bad = bytearray(legacy(1, b"abc"))
            bad[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(exporter.ExportError):
                exporter.recover_files(image(bad))
        bad = bytearray(legacy(1, b"abc"))
        bad[6:8] = (2048).to_bytes(2, "little")
        bad[10:12] = crc(bad[:10])
        with self.assertRaises(exporter.ExportError):
            exporter.recover_files(image(bad))

    def test_blank_storage_is_distinct_from_corrupt_storage(self) -> None:
        for size in (exporter.IMAGE_SIZE, *exporter.LEGACY_IMAGE_SIZES):
            self.assertIsNone(exporter.recover_files(image(size=size)))
        corrupt = bytearray(image())
        corrupt[exporter.SNAPSHOT_OFFSETS[1] + 512] = 1
        with self.assertRaises(exporter.ExportError):
            exporter.recover_files(corrupt)

    def test_rejects_wrong_image_sizes_and_missing_boot_signature(self) -> None:
        data = image(snapshot(1, ()))
        for bad in (b"", data[:-1], data + b"\0", b"\0" * len(data)):
            with self.assertRaises(exporter.ExportError):
                exporter.recover_files(bad)


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="nixodria-export-test-")
        self.addCleanup(self.temporary.cleanup)
        # On macOS /var is itself a symlink; give the tool an explicit canonical path.
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "legacy.img"
        self.destination = self.root / "exported"
        self.files = ((b"HELLO.BAS", b'10 PRINT "HELLO"\r\n20 END\r\n'), (b"DATA.TXT", b"\0\xff"))
        self.original = image(snapshot(3, self.files))
        self.source.write_bytes(self.original)

    def test_export_creates_private_files_and_does_not_change_source(self) -> None:
        result = exporter.export_files(self.source, self.destination)
        self.assertEqual(result.files, self.files)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o700)
        self.assertEqual({p.name for p in self.destination.iterdir()}, {"HELLO.BAS", "DATA.TXT"})
        for name, data in self.files:
            output = self.destination / name.decode("ascii")
            self.assertEqual(output.read_bytes(), data)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_refuses_existing_destination_and_keeps_existing_files(self) -> None:
        self.destination.mkdir()
        saved = self.destination / "HELLO.BAS"
        saved.write_bytes(b"do not overwrite")
        with self.assertRaises(FileExistsError):
            exporter.export_files(self.source, self.destination)
        self.assertEqual(saved.read_bytes(), b"do not overwrite")
        self.assertEqual(list(self.destination.iterdir()), [saved])

    def test_refuses_destination_that_is_an_existing_file(self) -> None:
        self.destination.write_bytes(b"keep this file")
        with self.assertRaises(FileExistsError):
            exporter.export_files(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"keep this file")

    def test_blank_image_exports_an_empty_directory(self) -> None:
        self.source.write_bytes(image())
        self.assertIsNone(exporter.export_files(self.source, self.destination))
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_refuses_destination_symlink(self) -> None:
        target = self.root / "target"
        target.mkdir()
        self.destination.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            exporter.export_files(self.source, self.destination)
        self.assertTrue(self.destination.is_symlink())
        self.assertEqual(list(target.iterdir()), [])

    def test_refuses_source_symlink(self) -> None:
        link = self.root / "link.img"
        link.symlink_to(self.source)
        with self.assertRaises(OSError):
            exporter.export_files(link, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_refuses_symlink_parent_components(self) -> None:
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            exporter.export_files(self.source, link / "exported")
        with self.assertRaises(OSError):
            exporter.export_files(link / "legacy.img", self.destination)
        self.assertFalse(self.destination.exists())

    def test_invalid_source_does_not_create_destination(self) -> None:
        self.source.write_bytes(b"invalid")
        with self.assertRaises(exporter.ExportError):
            exporter.export_files(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source.read_bytes(), b"invalid")

    def test_failed_write_cleans_up_only_new_export(self) -> None:
        with patch.object(exporter.os, "fsync", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                exporter.export_files(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
