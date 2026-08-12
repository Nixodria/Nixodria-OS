#!/usr/bin/env python3
"""Check the invariants required for a BIOS boot sector."""

from pathlib import Path
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} IMAGE", file=sys.stderr)
        return 2

    image = Path(sys.argv[1])
    data = image.read_bytes()

    if len(data) != 512:
        print(f"check: expected 512 bytes, found {len(data)}", file=sys.stderr)
        return 1
    if data[-2:] != b"\x55\xaa":
        print("check: missing BIOS boot signature 55 aa", file=sys.stderr)
        return 1
    if b"Nixodria OS" not in data or b"nix> " not in data:
        print("check: expected console strings are missing", file=sys.stderr)
        return 1

    print("check: 512-byte BIOS boot sector with signature 55 aa")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
