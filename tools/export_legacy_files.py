#!/usr/bin/env python3
"""Recover saved files from a supplied legacy Nixodria disk without modifying it.

Only saved storage records are exported, never the bundled package catalog.
The destination must be a new directory under an existing, non-symlink parent.
BASIC source is copied byte for byte; this tool does not translate it to Rust.
"""

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import sys


SECTOR_SIZE = 512
IMAGE_SIZE = 2880 * SECTOR_SIZE
SNAPSHOT_SIZE = 33 * SECTOR_SIZE
SNAPSHOT_OFFSETS = (11 * SECTOR_SIZE, 44 * SECTOR_SIZE)
LEGACY_SLOT_SIZE = 5 * SECTOR_SIZE
LEGACY_IMAGE_SIZES = (14 * SECTOR_SIZE, 18 * SECTOR_SIZE)
FILE_CAPACITY = 2048
MAX_FILES = 8
ENTRY_OFFSET = 8
ENTRY_SIZE = 18
HEADER_CHECKSUM_OFFSET = 152


class ExportError(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    slot: int
    generation: int
    version: str
    files: tuple[tuple[bytes, bytes], ...]


def checksum16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE, as used by the original BIOS storage code."""
    checksum = 0xFFFF
    for value in data:
        checksum ^= value << 8
        for _ in range(8):
            checksum = ((checksum << 1) ^ (0x1021 if checksum & 0x8000 else 0)) & 0xFFFF
    return checksum


def word(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def parse_record(data: bytes, offset: int, slot: int, allow_nix3: bool) -> Snapshot | None:
    header = data[offset : offset + SECTOR_SIZE]
    if len(header) != SECTOR_SIZE:
        return None
    generation = word(header, 4)
    if header[:4] == b"NIX2":
        length = word(header, 6)
        if length >= FILE_CAPACITY or checksum16(header[:10]) != word(header, 10):
            return None
        payload = data[offset + SECTOR_SIZE : offset + SECTOR_SIZE + length]
        if len(payload) != length or checksum16(payload) != word(header, 8):
            return None
        return Snapshot(slot, generation, "NIX2", ((b"UNTITLED.TXT", payload),))
    if not allow_nix3 or header[:4] != b"NIX3":
        return None
    if header[6] > MAX_FILES or header[7] != 0:
        return None
    if checksum16(header[:HEADER_CHECKSUM_OFFSET]) != word(header, HEADER_CHECKSUM_OFFSET):
        return None

    files: list[tuple[bytes, bytes]] = []
    names: set[bytes] = set()
    for index in range(header[6]):
        entry = ENTRY_OFFSET + index * ENTRY_SIZE
        name, separator, padding = header[entry : entry + 13].partition(b"\0")
        if (
            not separator or not name or name in (b".", b"..")
            or any(padding) or header[entry + 13] != 0 or name in names
            or any(not (65 <= c <= 90 or 48 <= c <= 57 or c in b"._-") for c in name)
        ):
            return None
        length = word(header, entry + 14)
        if length >= FILE_CAPACITY:
            return None
        start = offset + SECTOR_SIZE + index * FILE_CAPACITY
        payload = data[start : start + length]
        if len(payload) != length or checksum16(payload) != word(header, entry + 16):
            return None
        names.add(name)
        files.append((name, payload))
    return Snapshot(slot, generation, "NIX3", tuple(files))


def recover_files(data: bytes) -> Snapshot | None:
    """Pick the newest entirely valid snapshot; None means an unused blank disk."""
    if len(data) == IMAGE_SIZE:
        offsets = SNAPSHOT_OFFSETS
        size = SNAPSHOT_SIZE
        allow_nix3 = True
    elif len(data) in LEGACY_IMAGE_SIZES:
        offsets = (len(data) - 2 * LEGACY_SLOT_SIZE, len(data) - LEGACY_SLOT_SIZE)
        size = LEGACY_SLOT_SIZE
        allow_nix3 = False
    else:
        raise ExportError("unsupported disk size; expected a 1.44 MiB floppy or a 14/18-sector legacy image")
    if data[510:512] != b"\x55\xaa":
        raise ExportError("disk has no BIOS boot signature")
    first = parse_record(data, offsets[0], 0, allow_nix3)
    second = parse_record(data, offsets[1], 1, allow_nix3)
    if first is None and second is None:
        if all(not any(data[offset : offset + size]) for offset in offsets):
            return None
        raise ExportError("neither storage snapshot is valid; nothing was exported")
    if first is None:
        return second
    if second is None:
        return first
    # Match the BIOS kernel, including wraparound, equal generations, and half range.
    return first if ((first.generation - second.generation) & 0xFFFF) <= 0x8000 else second


def open_parent(path: Path) -> tuple[int, str]:
    """Walk existing parent directories using file descriptors, refusing symlinks."""
    path = Path(os.path.abspath(path))
    if path.name in ("", ".", ".."):
        raise ExportError("a file or directory name is required")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        return parent, path.name
    except BaseException:
        os.close(parent)
        raise


def read_legacy_image(path: Path) -> bytes:
    parent, name = open_parent(path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ExportError("source image must be a regular file")
            data = source.read(IMAGE_SIZE + 1)
    finally:
        os.close(parent)
    return data


def export_files(image: Path, destination: Path) -> Snapshot | None:
    """Export into a newly created private directory; existing paths are untouched."""
    snapshot = recover_files(read_legacy_image(image))
    files = snapshot.files if snapshot else ()
    parent, name = open_parent(destination)
    directory = None
    created = False
    written: list[str] = []
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
        created = True
        directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        for filename, content in files:
            decoded = filename.decode("ascii")
            fd = os.open(decoded, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            written.append(decoded)
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        os.fsync(directory)
    except BaseException:
        # Only remove files created by this invocation in its newly created directory.
        if directory is not None:
            for filename in written:
                try:
                    os.unlink(filename, dir_fd=directory)
                except OSError:
                    pass
        if created:
            try:
                os.rmdir(name, dir_fd=parent)
            except OSError:
                pass
        raise
    finally:
        if directory is not None:
            os.close(directory)
        os.close(parent)
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="existing legacy disk image (read only)")
    parser.add_argument("destination", type=Path, help="new export directory (must not exist)")
    args = parser.parse_args()
    try:
        snapshot = export_files(args.image, args.destination)
    except (ExportError, OSError) as error:
        print(f"legacy export: {error}", file=sys.stderr)
        return 1
    if snapshot is None:
        print(f"legacy export: blank disk; created empty directory {args.destination}")
    else:
        print(f"legacy export: copied {len(snapshot.files)} files from {snapshot.version} "
              f"slot {'AB'[snapshot.slot]}, generation {snapshot.generation}, to {args.destination}")
    print("The original image is unchanged. BASIC files remain BASIC source; no translation was performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
