#!/usr/bin/env python3
"""Check the invariants required for the BIOS-loaded Nixodria image."""

from pathlib import Path
import sys


SECTOR_SIZE = 512
SYSTEM_SECTORS = 8
STORAGE_SECTORS = 10
IMAGE_SECTORS = SYSTEM_SECTORS + STORAGE_SECTORS
IMAGE_SIZE = SECTOR_SIZE * IMAGE_SECTORS


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
        b"Ctrl-S save",
        b"Ctrl-R run",
        b"Nixodria BASIC",
        b"BASIC error at line",
        b"Saved.",
        b"Save failed.",
        b"NIX2",
        b"Disk error",
    )
    system = data[: SYSTEM_SECTORS * SECTOR_SIZE]
    storage = data[SYSTEM_SECTORS * SECTOR_SIZE :]
    if any(value not in system for value in required_strings):
        print("check: expected console strings are missing", file=sys.stderr)
        return 1
    if any(storage):
        print("check: newly built persistent storage is not blank", file=sys.stderr)
        return 1

    print("check: eighteen-sector BIOS image with two blank save records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
