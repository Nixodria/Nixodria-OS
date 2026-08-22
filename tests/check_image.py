#!/usr/bin/env python3
"""Check the invariants required for the BIOS-loaded Nixodria image."""

from pathlib import Path
import re
import sys


SECTOR_SIZE = 512
IMAGE_SECTORS = 2880
SYSTEM_SECTORS = 11
SNAPSHOT_SECTORS = 33
STORAGE_SECTORS = SNAPSHOT_SECTORS * 2
PRINT_MODULE_SECTORS = 32
BASIC_MODULE_SECTORS = 16
APP_SLOT_SECTORS = 5
IMAGE_SIZE = SECTOR_SIZE * IMAGE_SECTORS
STORAGE_OFFSET = SECTOR_SIZE * SYSTEM_SECTORS
STORAGE_END = STORAGE_OFFSET + SECTOR_SIZE * STORAGE_SECTORS
PRINT_OFFSET = STORAGE_END
PRINT_END = PRINT_OFFSET + SECTOR_SIZE * PRINT_MODULE_SECTORS
BASIC_OFFSET = PRINT_END
BASIC_END = BASIC_OFFSET + SECTOR_SIZE * BASIC_MODULE_SECTORS
APP_OFFSET = BASIC_END
APP_END = APP_OFFSET + SECTOR_SIZE * APP_SLOT_SECTORS
MODULE_SIGNATURE_OFFSET = 3
MODULE_CHECKSUM_OFFSET = 12
APP_SIGNATURE = b"NIXAPP1"
APP_SOURCE_LENGTH_OFFSET = 8
APP_SOURCE_CHECKSUM_OFFSET = 10
APP_FILENAME_OFFSET = 16
APP_FILENAME_SIZE = 13
APP_FILENAME = b"TETRIS.BAS"
APP_SOURCE_MAX = (APP_SLOT_SECTORS - 1) * SECTOR_SIZE - 1
TETRIS_SOURCE = Path(__file__).resolve().parents[1] / "apps" / "TETRIS.BAS"


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


def normalize_basic_source(source: bytes) -> bytes:
    if b"\r" in source:
        raise ValueError("tracked TETRIS.BAS contains a carriage return")
    if b"\0" in source:
        raise ValueError("tracked TETRIS.BAS contains a NUL byte")
    if any(value > 0x7F for value in source):
        raise ValueError("tracked TETRIS.BAS is not ASCII")
    normalized = source.replace(b"\n", b"\r\n")
    if len(normalized) > APP_SOURCE_MAX:
        raise ValueError(
            f"normalized TETRIS.BAS is {len(normalized)} bytes; "
            f"maximum is {APP_SOURCE_MAX}"
        )
    return normalized


def validate_basic_program(source: bytes) -> None:
    lines = source.split(b"\r\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if not lines:
        raise ValueError("tracked TETRIS.BAS has no program lines")

    previous_number = -1
    line_numbers: set[int] = set()
    for physical_line, line in enumerate(lines, start=1):
        if not line:
            raise ValueError(
                f"tracked TETRIS.BAS has a blank physical line at {physical_line}"
            )
        number, separator, statement = line.partition(b" ")
        if (
            not separator
            or not number
            or any(value < ord("0") or value > ord("9") for value in number)
        ):
            raise ValueError(
                f"tracked TETRIS.BAS line {physical_line} must start with a "
                "decimal line number followed by a space"
            )
        if not statement:
            raise ValueError(
                f"tracked TETRIS.BAS line {physical_line} has no statement"
            )
        line_number = int(number)
        if line_number > 0xFFFF:
            raise ValueError(
                f"tracked TETRIS.BAS line {physical_line} has a line number "
                "above 65535"
            )
        if line_number <= previous_number:
            raise ValueError(
                "tracked TETRIS.BAS line numbers are not strictly increasing "
                f"at physical line {physical_line}"
            )
        previous_number = line_number
        line_numbers.add(line_number)

    targets = {
        int(match)
        for match in re.findall(
            rb"\b(?:goto|gosub|then)\s+([0-9]+)\b", source, re.IGNORECASE
        )
    }
    missing_targets = sorted(targets - line_numbers)
    if missing_targets:
        rendered = ", ".join(str(value) for value in missing_targets)
        raise ValueError(
            f"tracked TETRIS.BAS has missing control-flow targets: {rendered}"
        )


def module_error(module: bytes, signature: bytes, label: str) -> str | None:
    if (
        module[
            MODULE_SIGNATURE_OFFSET : MODULE_SIGNATURE_OFFSET + len(signature)
        ]
        != signature
    ):
        return f"{label} module is missing"
    stored_checksum = int.from_bytes(
        module[MODULE_CHECKSUM_OFFSET : MODULE_CHECKSUM_OFFSET + 2], "little"
    )
    unchecked_module = bytearray(module)
    unchecked_module[
        MODULE_CHECKSUM_OFFSET : MODULE_CHECKSUM_OFFSET + 2
    ] = b"\0\0"
    if stored_checksum != checksum16(unchecked_module):
        return f"{label} module checksum is invalid"
    return None


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
        b"run <filename>",
        b"Try: run TETRIS.BAS",
        b"print <filename>",
        b"Print job queued.",
        b"Printer unavailable.",
        b"BASIC runtime unavailable.",
        b"Ctrl-S save",
        b"Ctrl-R run",
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
    print_module = data[PRINT_OFFSET:PRINT_END]
    module_strings = (
        b"POST /ipp/print HTTP/1.1",
        b"image/pwg-raster",
        b"PwgRaster",
        b"RaS2",
    )
    error = module_error(print_module, b"NIXPRINT1", "native printer")
    if error is not None:
        print(f"check: {error}", file=sys.stderr)
        return 1
    if any(value not in print_module for value in module_strings):
        print("check: native printer protocol strings are missing", file=sys.stderr)
        return 1

    basic_module = data[BASIC_OFFSET:BASIC_END]
    error = module_error(basic_module, b"NIXBASIC1", "BASIC")
    if error is not None:
        print(f"check: {error}", file=sys.stderr)
        return 1
    basic_module_strings = (
        b"Nixodria BASIC",
        b"BASIC error at line",
        b"Program finished. Press any key.",
        b"gosub",
        b"return",
        b"timer",
        b"wait",
    )
    if any(value not in basic_module for value in basic_module_strings):
        print("check: expected BASIC runtime strings are missing", file=sys.stderr)
        return 1

    try:
        expected_source = normalize_basic_source(TETRIS_SOURCE.read_bytes())
        validate_basic_program(expected_source)
    except (OSError, ValueError) as error:
        print(f"check: {error}", file=sys.stderr)
        return 1

    app = data[APP_OFFSET:APP_END]
    header = app[:SECTOR_SIZE]
    payload = app[SECTOR_SIZE:]
    if header[: len(APP_SIGNATURE)] != APP_SIGNATURE:
        print("check: bundled BASIC app header is missing", file=sys.stderr)
        return 1
    if header[len(APP_SIGNATURE)] != 0 or any(header[12:16]):
        print(
            "check: bundled BASIC app reserved header bytes are not blank",
            file=sys.stderr,
        )
        return 1
    if header[
        APP_FILENAME_OFFSET : APP_FILENAME_OFFSET + APP_FILENAME_SIZE
    ] != APP_FILENAME.ljust(APP_FILENAME_SIZE, b"\0"):
        print("check: bundled BASIC app filename is invalid", file=sys.stderr)
        return 1
    if any(header[APP_FILENAME_OFFSET + APP_FILENAME_SIZE :]):
        print("check: bundled BASIC app unused header bytes are not blank", file=sys.stderr)
        return 1
    source_length = int.from_bytes(
        header[APP_SOURCE_LENGTH_OFFSET : APP_SOURCE_LENGTH_OFFSET + 2], "little"
    )
    if source_length > APP_SOURCE_MAX:
        print(
            "check: bundled BASIC app source length is out of bounds",
            file=sys.stderr,
        )
        return 1
    if source_length != len(expected_source):
        print(
            "check: bundled BASIC app source length does not match TETRIS.BAS",
            file=sys.stderr,
        )
        return 1
    stored_source_checksum = int.from_bytes(
        header[
            APP_SOURCE_CHECKSUM_OFFSET : APP_SOURCE_CHECKSUM_OFFSET + 2
        ],
        "little",
    )
    if stored_source_checksum != checksum16(expected_source):
        print(
            "check: bundled BASIC app source checksum is invalid",
            file=sys.stderr,
        )
        return 1
    if payload[:source_length] != expected_source:
        print(
            "check: bundled BASIC app source does not match TETRIS.BAS",
            file=sys.stderr,
        )
        return 1
    if any(payload[source_length:]):
        print("check: bundled BASIC app payload padding is not blank", file=sys.stderr)
        return 1
    if any(data[APP_END:]):
        print("check: unused floppy sectors are not blank", file=sys.stderr)
        return 1

    print(
        "check: 1.44 MB BIOS floppy with two blank snapshots, native print and "
        "BASIC modules, and editable TETRIS.BAS"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
