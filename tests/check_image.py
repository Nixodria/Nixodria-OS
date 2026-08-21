#!/usr/bin/env python3
"""Check the invariants required for the BIOS-loaded Nixodria image."""

from pathlib import Path
import sys


SECTOR_SIZE = 512
IMAGE_SECTORS = 2880
SYSTEM_SECTORS = 11
SNAPSHOT_SECTORS = 33
STORAGE_SECTORS = SNAPSHOT_SECTORS * 2
IMAGE_SIZE = SECTOR_SIZE * IMAGE_SECTORS
STORAGE_OFFSET = SECTOR_SIZE * SYSTEM_SECTORS
STORAGE_END = STORAGE_OFFSET + SECTOR_SIZE * STORAGE_SECTORS


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} IMAGE", file=sys.stderr)
        return 2

    image = Path(sys.argv[1])
    data = image.read_bytes()

    if len(data) != IMAGE_SIZE:
        print(f"check: expected {IMAGE_SIZE} bytes, found {len(data)}", file=sys.stderr)
        return 1
    if data[SECTOR_SIZE - 2 : SECTOR_SIZE] != b"\x55\xaa":
        print("check: first sector is missing BIOS signature 55 aa", file=sys.stderr)
        return 1
    required_strings = (
        b"Nixodria OS",
        b"nix> ",
        b"Nixodria Editor",
        b"Files:",
        b"No files.",
        b"Invalid filename.",
        b"Ctrl-S save",
        b"Ctrl-R run",
        b"Nixodria BASIC",
        b"BASIC error at line",
        b"Saved.",
        b"Save failed.",
        b"Storage full.",
        b"NIX3",
        b"NIX2",
        b"Disk error",
    )
    system = data[: SYSTEM_SECTORS * SECTOR_SIZE]
    storage = data[STORAGE_OFFSET:STORAGE_END]
    if any(value not in system for value in required_strings):
        print("check: expected console strings are missing", file=sys.stderr)
        return 1
    if any(storage):
        print("check: newly built file snapshots are not blank", file=sys.stderr)
        return 1
    if any(data[STORAGE_END:]):
        print("check: unused floppy sectors are not blank", file=sys.stderr)
        return 1

    print("check: 1.44 MB BIOS floppy with two blank file snapshots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
