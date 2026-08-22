#!/usr/bin/env python3
"""Install Nixodria's native printer overlay into a blank floppy image."""

from pathlib import Path
import sys


SECTOR_SIZE = 512
IMAGE_SECTORS = 2880
SYSTEM_SECTORS = 11
SNAPSHOT_SECTORS = 33
SNAPSHOT_COUNT = 2
PRINT_MODULE_SECTORS = 32
IMAGE_SIZE = SECTOR_SIZE * IMAGE_SECTORS
PRINT_OFFSET = SECTOR_SIZE * (
    SYSTEM_SECTORS + SNAPSHOT_COUNT * SNAPSHOT_SECTORS
)
PRINT_SIZE = SECTOR_SIZE * PRINT_MODULE_SECTORS
PRINT_END = PRINT_OFFSET + PRINT_SIZE
PRINT_SIGNATURE_OFFSET = 3
PRINT_CHECKSUM_OFFSET = 12
PRINT_SIGNATURE = b"NIXPRINT1"


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


def build_image(base_path: Path, module_path: Path, output_path: Path) -> str:
    base = base_path.read_bytes()
    module = module_path.read_bytes()
    if len(base) != IMAGE_SIZE:
        raise ImageError(
            f"base image is {len(base)} bytes; expected {IMAGE_SIZE}"
        )
    if base[SECTOR_SIZE - 2 : SECTOR_SIZE] != b"\x55\xaa":
        raise ImageError("base image has no BIOS signature")
    if any(base[PRINT_OFFSET:PRINT_END]):
        raise ImageError("base image printer-module region is not blank")
    if not module:
        raise ImageError("printer module is empty")
    if len(module) > PRINT_SIZE:
        raise ImageError(
            f"printer module is {len(module)} bytes; maximum is {PRINT_SIZE}"
        )
    if (
        module[PRINT_SIGNATURE_OFFSET : PRINT_SIGNATURE_OFFSET + 9]
        != PRINT_SIGNATURE
    ):
        raise ImageError("printer module has no fixed NIXPRINT1 header")
    if module[PRINT_CHECKSUM_OFFSET : PRINT_CHECKSUM_OFFSET + 2] != b"\0\0":
        raise ImageError("printer module checksum field is not blank")

    image = bytearray(base)
    module_slot = bytearray(PRINT_SIZE)
    module_slot[: len(module)] = module
    checksum = checksum16(module_slot)
    module_slot[PRINT_CHECKSUM_OFFSET : PRINT_CHECKSUM_OFFSET + 2] = (
        checksum.to_bytes(2, "little")
    )
    image[PRINT_OFFSET:PRINT_END] = module_slot
    output_path.write_bytes(image)
    return (
        f"image: installed {len(module)}-byte native printer module "
        f"(CRC-16 {checksum:04x}) in {output_path}"
    )


def main() -> int:
    if len(sys.argv) != 4:
        print(
            f"usage: {Path(sys.argv[0]).name} BASE MODULE OUTPUT",
            file=sys.stderr,
        )
        return 2
    try:
        message = build_image(
            Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
        )
    except (ImageError, OSError) as error:
        print(f"image: {error}", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
