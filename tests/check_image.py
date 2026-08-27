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
PACKAGE_SLOT_SECTORS = 5
PACKAGE_SLOTS = 8
IMAGE_SIZE = SECTOR_SIZE * IMAGE_SECTORS
STORAGE_OFFSET = SECTOR_SIZE * SYSTEM_SECTORS
STORAGE_END = STORAGE_OFFSET + SECTOR_SIZE * STORAGE_SECTORS
PRINT_OFFSET = STORAGE_END
PRINT_END = PRINT_OFFSET + SECTOR_SIZE * PRINT_MODULE_SECTORS
BASIC_OFFSET = PRINT_END
BASIC_END = BASIC_OFFSET + SECTOR_SIZE * BASIC_MODULE_SECTORS
PACKAGE_OFFSET = BASIC_END
PACKAGE_SLOT_SIZE = SECTOR_SIZE * PACKAGE_SLOT_SECTORS
PACKAGE_END = PACKAGE_OFFSET + PACKAGE_SLOT_SIZE * PACKAGE_SLOTS
MODULE_SIGNATURE_OFFSET = 3
MODULE_CHECKSUM_OFFSET = 12
PACKAGE_SIGNATURE = b"NIXPKG1\0"
PACKAGE_SOURCE_LENGTH_OFFSET = 8
PACKAGE_SOURCE_CHECKSUM_OFFSET = 10
PACKAGE_HEADER_CHECKSUM_OFFSET = 12
PACKAGE_FILENAME_OFFSET = 16
PACKAGE_FILENAME_SIZE = 13
PACKAGE_SOURCE_MAX = (PACKAGE_SLOT_SECTORS - 1) * SECTOR_SIZE - 1


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


def validate_basic_program(source: bytes, label: str) -> None:
    lines = source.split(b"\r\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if not lines:
        raise ValueError(f"package {label} has no program lines")

    previous_number = -1
    line_numbers: set[int] = set()
    for physical_line, line in enumerate(lines, start=1):
        if not line:
            raise ValueError(
                f"package {label} has a blank physical line at {physical_line}"
            )
        number, separator, statement = line.partition(b" ")
        if (
            not separator
            or not number
            or any(value < ord("0") or value > ord("9") for value in number)
        ):
            raise ValueError(
                f"package {label} line {physical_line} must start with a "
                "decimal line number followed by a space"
            )
        if not statement:
            raise ValueError(
                f"package {label} line {physical_line} has no statement"
            )
        line_number = int(number)
        if line_number > 0xFFFF:
            raise ValueError(
                f"package {label} line {physical_line} has a line number "
                "above 65535"
            )
        if line_number <= previous_number:
            raise ValueError(
                f"package {label} line numbers are not strictly increasing "
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
            f"package {label} has missing control-flow targets: {rendered}"
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
        b"pkg list",
        b"pkg install <filename>",
        b"pkg remove <filename>",
        b"Packages:",
        b"Package installed.",
        b"Package removed.",
        b"Package already installed.",
        b"Package not found.",
        b"Package is not installed.",
        b"Package catalog unavailable.",
        b"Package install failed.",
        b"Package remove failed.",
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

    package_names: list[bytes] = []
    found_empty = False
    for slot_index in range(PACKAGE_SLOTS):
        start = PACKAGE_OFFSET + slot_index * PACKAGE_SLOT_SIZE
        slot = data[start : start + PACKAGE_SLOT_SIZE]
        if not any(slot):
            found_empty = True
            continue
        if found_empty:
            print(
                "check: populated package follows an empty catalog slot",
                file=sys.stderr,
            )
            return 1

        header = slot[:SECTOR_SIZE]
        payload = slot[SECTOR_SIZE:]
        if header[: len(PACKAGE_SIGNATURE)] != PACKAGE_SIGNATURE:
            print(
                f"check: package slot {slot_index} header is invalid",
                file=sys.stderr,
            )
            return 1
        if any(header[14:16]) or any(
            header[PACKAGE_FILENAME_OFFSET + PACKAGE_FILENAME_SIZE :]
        ):
            print(
                f"check: package slot {slot_index} reserved bytes are not blank",
                file=sys.stderr,
            )
            return 1
        stored_header_checksum = int.from_bytes(
            header[
                PACKAGE_HEADER_CHECKSUM_OFFSET : PACKAGE_HEADER_CHECKSUM_OFFSET + 2
            ],
            "little",
        )
        unchecked_header = bytearray(header)
        unchecked_header[
            PACKAGE_HEADER_CHECKSUM_OFFSET : PACKAGE_HEADER_CHECKSUM_OFFSET + 2
        ] = b"\0\0"
        if stored_header_checksum != checksum16(unchecked_header):
            print(
                f"check: package slot {slot_index} header checksum is invalid",
                file=sys.stderr,
            )
            return 1

        field = header[
            PACKAGE_FILENAME_OFFSET : PACKAGE_FILENAME_OFFSET + PACKAGE_FILENAME_SIZE
        ]
        name, separator, padding = field.partition(b"\0")
        if (
            not separator
            or not name
            or padding.strip(b"\0")
            or len(name) > PACKAGE_FILENAME_SIZE - 1
            or not name.endswith(b".BAS")
            or any(
                not (
                    ord("A") <= value <= ord("Z")
                    or ord("0") <= value <= ord("9")
                    or value in b"._-"
                )
                for value in name
            )
        ):
            print(
                f"check: package slot {slot_index} filename is invalid",
                file=sys.stderr,
            )
            return 1
        if name in package_names:
            print(
                f"check: duplicate package {name.decode('ascii')}",
                file=sys.stderr,
            )
            return 1
        package_names.append(name)

        source_length = int.from_bytes(
            header[
                PACKAGE_SOURCE_LENGTH_OFFSET : PACKAGE_SOURCE_LENGTH_OFFSET + 2
            ],
            "little",
        )
        if source_length > PACKAGE_SOURCE_MAX:
            print(
                f"check: package {name.decode('ascii')} is too large",
                file=sys.stderr,
            )
            return 1
        source = payload[:source_length]
        if (
            not source
            or b"\0" in source
            or any(value > 0x7F for value in source)
            or b"\n" in source.replace(b"\r\n", b"")
            or b"\r" in source.replace(b"\r\n", b"")
        ):
            print(
                f"check: package {name.decode('ascii')} source is invalid",
                file=sys.stderr,
            )
            return 1
        stored_source_checksum = int.from_bytes(
            header[
                PACKAGE_SOURCE_CHECKSUM_OFFSET : PACKAGE_SOURCE_CHECKSUM_OFFSET + 2
            ],
            "little",
        )
        if stored_source_checksum != checksum16(source):
            print(
                f"check: package {name.decode('ascii')} checksum is invalid",
                file=sys.stderr,
            )
            return 1
        if any(payload[source_length:]):
            print(
                f"check: package {name.decode('ascii')} padding is not blank",
                file=sys.stderr,
            )
            return 1
        try:
            validate_basic_program(source, name.decode("ascii"))
        except ValueError as error:
            print(f"check: {error}", file=sys.stderr)
            return 1

    if not package_names:
        print("check: package catalog is empty", file=sys.stderr)
        return 1
    if any(data[PACKAGE_END:]):
        print("check: unused floppy sectors are not blank", file=sys.stderr)
        return 1

    rendered_packages = ", ".join(name.decode("ascii") for name in package_names)
    print(
        "check: 1.44 MB BIOS floppy with two blank snapshots, native print and "
        f"BASIC modules, and packages {rendered_packages}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
