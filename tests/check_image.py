#!/usr/bin/env python3
"""Check the invariants required for the BIOS-loaded Nixodria image."""

from pathlib import Path
import sys


SECTOR_SIZE = 512
IMAGE_SECTORS = 3
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
        b"Ctrl-X exit",
        b"Disk error",
    )
    if any(value not in data for value in required_strings):
        print("check: expected console strings are missing", file=sys.stderr)
        return 1

    print("check: three-sector BIOS image with loader signature 55 aa")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
