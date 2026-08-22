#!/usr/bin/env python3
"""Install Nixodria's immutable modules and bundled app into its floppy image."""

from pathlib import Path
import sys


SECTOR_SIZE = 512
IMAGE_SECTORS = 2880
SYSTEM_SECTORS = 11
SNAPSHOT_SECTORS = 33
SNAPSHOT_COUNT = 2
PRINT_MODULE_SECTORS = 32
BASIC_MODULE_SECTORS = 16
APP_SLOT_SECTORS = 5
IMAGE_SIZE = SECTOR_SIZE * IMAGE_SECTORS
PRINT_OFFSET = SECTOR_SIZE * (
    SYSTEM_SECTORS + SNAPSHOT_COUNT * SNAPSHOT_SECTORS
)
PRINT_SIZE = SECTOR_SIZE * PRINT_MODULE_SECTORS
PRINT_END = PRINT_OFFSET + PRINT_SIZE
BASIC_OFFSET = PRINT_END
BASIC_SIZE = SECTOR_SIZE * BASIC_MODULE_SECTORS
BASIC_END = BASIC_OFFSET + BASIC_SIZE
APP_OFFSET = BASIC_END
APP_SIZE = SECTOR_SIZE * APP_SLOT_SECTORS
APP_END = APP_OFFSET + APP_SIZE
MODULE_SIGNATURE_OFFSET = 3
MODULE_CHECKSUM_OFFSET = 12
PRINT_SIGNATURE = b"NIXPRINT1"
BASIC_SIGNATURE = b"NIXBASIC1"
APP_SIGNATURE = b"NIXAPP1"
APP_SOURCE_LENGTH_OFFSET = 8
APP_SOURCE_CHECKSUM_OFFSET = 10
APP_FILENAME_OFFSET = 16
APP_FILENAME_SIZE = 13
APP_FILENAME = b"TETRIS.BAS"
APP_SOURCE_MAX = (APP_SLOT_SECTORS - 1) * SECTOR_SIZE - 1


class ImageError(RuntimeError):
    pass


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
        raise ImageError("bundled BASIC source contains a carriage return")
    if b"\0" in source:
        raise ImageError("bundled BASIC source contains a NUL byte")
    if any(value > 0x7F for value in source):
        raise ImageError("bundled BASIC source is not ASCII")
    normalized = source.replace(b"\n", b"\r\n")
    if len(normalized) > APP_SOURCE_MAX:
        raise ImageError(
            f"normalized BASIC source is {len(normalized)} bytes; "
            f"maximum is {APP_SOURCE_MAX}"
        )
    return normalized


def build_module_slot(
    module: bytes, size: int, signature: bytes, label: str
) -> tuple[bytes, int]:
    if not module:
        raise ImageError(f"{label} module is empty")
    if len(module) > size:
        raise ImageError(
            f"{label} module is {len(module)} bytes; maximum is {size}"
        )
    if (
        module[MODULE_SIGNATURE_OFFSET : MODULE_SIGNATURE_OFFSET + len(signature)]
        != signature
    ):
        rendered_signature = signature.decode("ascii")
        raise ImageError(
            f"{label} module has no fixed {rendered_signature} header"
        )
    if (
        module[MODULE_CHECKSUM_OFFSET : MODULE_CHECKSUM_OFFSET + 2]
        != b"\0\0"
    ):
        raise ImageError(f"{label} module checksum field is not blank")

    slot = bytearray(size)
    slot[: len(module)] = module
    checksum = checksum16(slot)
    slot[MODULE_CHECKSUM_OFFSET : MODULE_CHECKSUM_OFFSET + 2] = checksum.to_bytes(
        2, "little"
    )
    return bytes(slot), checksum


def build_app_slot(source: bytes) -> tuple[bytes, int, int]:
    normalized = normalize_basic_source(source)
    checksum = checksum16(normalized)
    slot = bytearray(APP_SIZE)
    slot[: len(APP_SIGNATURE)] = APP_SIGNATURE
    slot[
        APP_SOURCE_LENGTH_OFFSET : APP_SOURCE_LENGTH_OFFSET + 2
    ] = len(normalized).to_bytes(2, "little")
    slot[
        APP_SOURCE_CHECKSUM_OFFSET : APP_SOURCE_CHECKSUM_OFFSET + 2
    ] = checksum.to_bytes(2, "little")
    slot[
        APP_FILENAME_OFFSET : APP_FILENAME_OFFSET + APP_FILENAME_SIZE
    ] = APP_FILENAME.ljust(APP_FILENAME_SIZE, b"\0")
    slot[SECTOR_SIZE : SECTOR_SIZE + len(normalized)] = normalized
    return bytes(slot), checksum, len(normalized)


def build_image(
    base_path: Path,
    print_path: Path,
    basic_path: Path,
    source_path: Path,
    output_path: Path,
) -> str:
    base = base_path.read_bytes()
    print_module = print_path.read_bytes()
    basic_module = basic_path.read_bytes()
    source = source_path.read_bytes()
    if len(base) != IMAGE_SIZE:
        raise ImageError(
            f"base image is {len(base)} bytes; expected {IMAGE_SIZE}"
        )
    if base[SECTOR_SIZE - 2 : SECTOR_SIZE] != b"\x55\xaa":
        raise ImageError("base image has no BIOS signature")
    if any(base[PRINT_OFFSET:APP_END]):
        raise ImageError("base image immutable-module region is not blank")
    if any(base[APP_END:]):
        raise ImageError("base image unused floppy sectors are not blank")

    print_slot, print_checksum = build_module_slot(
        print_module, PRINT_SIZE, PRINT_SIGNATURE, "printer"
    )
    basic_slot, basic_checksum = build_module_slot(
        basic_module, BASIC_SIZE, BASIC_SIGNATURE, "BASIC"
    )
    app_slot, app_checksum, app_source_length = build_app_slot(source)

    image = bytearray(base)
    image[PRINT_OFFSET:PRINT_END] = print_slot
    image[BASIC_OFFSET:BASIC_END] = basic_slot
    image[APP_OFFSET:APP_END] = app_slot
    output_path.write_bytes(image)
    return (
        f"image: installed {len(print_module)}-byte printer module "
        f"(CRC-16 {print_checksum:04x}), {len(basic_module)}-byte BASIC module "
        f"(CRC-16 {basic_checksum:04x}), and {app_source_length}-byte TETRIS.BAS "
        f"(CRC-16 {app_checksum:04x}) in {output_path}"
    )


def main() -> int:
    if len(sys.argv) != 6:
        print(
            f"usage: {Path(sys.argv[0]).name} BASE PRINT_MODULE BASIC_MODULE "
            "TETRIS_SOURCE OUTPUT",
            file=sys.stderr,
        )
        return 2
    try:
        message = build_image(
            Path(sys.argv[1]),
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            Path(sys.argv[4]),
            Path(sys.argv[5]),
        )
    except (ImageError, OSError) as error:
        print(f"image: {error}", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
