#!/usr/bin/env python3
"""Check the invariants required for the BIOS-loaded Nixodria image."""

from pathlib import Path
import sys


SECTOR_SIZE = 512
IMAGE_SECTORS = 2880
SYSTEM_SECTORS = 11
SNAPSHOT_SECTORS = 33
STORAGE_SECTORS = SNAPSHOT_SECTORS * 2
PRINT_MODULE_SECTORS = 32
IMAGE_SIZE = SECTOR_SIZE * IMAGE_SECTORS
STORAGE_OFFSET = SECTOR_SIZE * SYSTEM_SECTORS
STORAGE_END = STORAGE_OFFSET + SECTOR_SIZE * STORAGE_SECTORS
PRINT_OFFSET = STORAGE_END
PRINT_END = PRINT_OFFSET + SECTOR_SIZE * PRINT_MODULE_SECTORS
PRINT_SIGNATURE_OFFSET = 3
PRINT_CHECKSUM_OFFSET = 12


def checksum16(data: bytes) -> int:
    checksum = 0xFFFF
    for value in data:
        checksum ^= value << 8
        for _ in range(8):
            if checksum & 0x8000:
                checksum = ((checksum << 1) ^ 0x1021) & 0xFFFF
            else:
                checksum = (checksum << 1) & 0xFFFF
    return checksum


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
        b"print <filename>",
        b"Print job queued.",
        b"Printer unavailable.",
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
    module = data[PRINT_OFFSET:PRINT_END]
    module_strings = (
        b"POST /ipp/print HTTP/1.1",
        b"image/pwg-raster",
        b"PwgRaster",
        b"RaS2",
    )
    if module[PRINT_SIGNATURE_OFFSET : PRINT_SIGNATURE_OFFSET + 9] != b"NIXPRINT1":
        print("check: native printer module is missing", file=sys.stderr)
        return 1
    stored_checksum = int.from_bytes(
        module[PRINT_CHECKSUM_OFFSET : PRINT_CHECKSUM_OFFSET + 2], "little"
    )
    unchecked_module = bytearray(module)
    unchecked_module[PRINT_CHECKSUM_OFFSET : PRINT_CHECKSUM_OFFSET + 2] = b"\0\0"
    if stored_checksum != checksum16(unchecked_module):
        print("check: native printer module checksum is invalid", file=sys.stderr)
        return 1
    if any(value not in module for value in module_strings):
        print("check: native printer protocol strings are missing", file=sys.stderr)
        return 1
    if any(data[PRINT_END:]):
        print("check: unused floppy sectors are not blank", file=sys.stderr)
        return 1

    print(
        "check: 1.44 MB BIOS floppy with two blank snapshots and native print module"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
