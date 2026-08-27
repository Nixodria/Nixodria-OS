#!/usr/bin/env python3
"""Install Nixodria's immutable modules and package catalog into its image."""

from pathlib import Path
import sys


SECTOR_SIZE = 512
IMAGE_SECTORS = 2880
SYSTEM_SECTORS = 11
SNAPSHOT_SECTORS = 33
SNAPSHOT_COUNT = 2
PRINT_MODULE_SECTORS = 32
BASIC_MODULE_SECTORS = 16
PACKAGE_SLOT_SECTORS = 5
PACKAGE_SLOTS = 8
IMAGE_SIZE = SECTOR_SIZE * IMAGE_SECTORS
PRINT_OFFSET = SECTOR_SIZE * (
    SYSTEM_SECTORS + SNAPSHOT_COUNT * SNAPSHOT_SECTORS
)
PRINT_SIZE = SECTOR_SIZE * PRINT_MODULE_SECTORS
PRINT_END = PRINT_OFFSET + PRINT_SIZE
BASIC_OFFSET = PRINT_END
BASIC_SIZE = SECTOR_SIZE * BASIC_MODULE_SECTORS
BASIC_END = BASIC_OFFSET + BASIC_SIZE
PACKAGE_OFFSET = BASIC_END
PACKAGE_SIZE = SECTOR_SIZE * PACKAGE_SLOT_SECTORS * PACKAGE_SLOTS
PACKAGE_END = PACKAGE_OFFSET + PACKAGE_SIZE
MODULE_SIGNATURE_OFFSET = 3
MODULE_CHECKSUM_OFFSET = 12
PRINT_SIGNATURE = b"NIXPRINT1"
BASIC_SIGNATURE = b"NIXBASIC1"
PACKAGE_SIGNATURE = b"NIXPKG1\0"
PACKAGE_SOURCE_LENGTH_OFFSET = 8
PACKAGE_SOURCE_CHECKSUM_OFFSET = 10
PACKAGE_HEADER_CHECKSUM_OFFSET = 12
PACKAGE_FILENAME_OFFSET = 16
PACKAGE_FILENAME_SIZE = 13
PACKAGE_SOURCE_MAX = (PACKAGE_SLOT_SECTORS - 1) * SECTOR_SIZE - 1


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


def package_filename(header: bytes, slot_index: int) -> bytes:
    field = header[
        PACKAGE_FILENAME_OFFSET : PACKAGE_FILENAME_OFFSET + PACKAGE_FILENAME_SIZE
    ]
    name, separator, padding = field.partition(b"\0")
    if not separator or not name or padding.strip(b"\0"):
        raise ImageError(f"package slot {slot_index} has an invalid filename field")
    if len(name) > PACKAGE_FILENAME_SIZE - 1 or not name.endswith(b".BAS"):
        raise ImageError(f"package slot {slot_index} has an invalid BASIC filename")
    if any(
        not (
            ord("A") <= value <= ord("Z")
            or ord("0") <= value <= ord("9")
            or value in b"._-"
        )
        for value in name
    ):
        raise ImageError(f"package slot {slot_index} has an invalid filename")
    return name


def validate_package_catalog(catalog: bytes) -> tuple[bytes, ...]:
    if len(catalog) != PACKAGE_SIZE:
        raise ImageError(
            f"package catalog is {len(catalog)} bytes; expected {PACKAGE_SIZE}"
        )

    names: list[bytes] = []
    found_empty = False
    slot_size = PACKAGE_SLOT_SECTORS * SECTOR_SIZE
    for slot_index in range(PACKAGE_SLOTS):
        start = slot_index * slot_size
        slot = catalog[start : start + slot_size]
        if not any(slot):
            found_empty = True
            continue
        if found_empty:
            raise ImageError("package catalog has a populated slot after an empty slot")

        header = slot[:SECTOR_SIZE]
        payload = slot[SECTOR_SIZE:]
        if header[: len(PACKAGE_SIGNATURE)] != PACKAGE_SIGNATURE:
            raise ImageError(f"package slot {slot_index} has an invalid signature")
        if any(header[14:16]) or any(
            header[PACKAGE_FILENAME_OFFSET + PACKAGE_FILENAME_SIZE :]
        ):
            raise ImageError(f"package slot {slot_index} has nonzero reserved bytes")

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
            raise ImageError(f"package slot {slot_index} header checksum is invalid")

        name = package_filename(header, slot_index)
        if name in names:
            raise ImageError(f"package catalog repeats {name.decode('ascii')}")
        names.append(name)

        source_length = int.from_bytes(
            header[
                PACKAGE_SOURCE_LENGTH_OFFSET : PACKAGE_SOURCE_LENGTH_OFFSET + 2
            ],
            "little",
        )
        if source_length > PACKAGE_SOURCE_MAX:
            raise ImageError(f"package {name.decode('ascii')} is too large")
        source = payload[:source_length]
        if not source or b"\0" in source or any(value > 0x7F for value in source):
            raise ImageError(f"package {name.decode('ascii')} is not ASCII BASIC source")
        if b"\n" in source.replace(b"\r\n", b"") or b"\r" in source.replace(
            b"\r\n", b""
        ):
            raise ImageError(
                f"package {name.decode('ascii')} does not use canonical CRLF lines"
            )
        stored_source_checksum = int.from_bytes(
            header[
                PACKAGE_SOURCE_CHECKSUM_OFFSET : PACKAGE_SOURCE_CHECKSUM_OFFSET + 2
            ],
            "little",
        )
        if stored_source_checksum != checksum16(source):
            raise ImageError(f"package {name.decode('ascii')} checksum is invalid")
        if any(payload[source_length:]):
            raise ImageError(f"package {name.decode('ascii')} padding is not blank")

    if not names:
        raise ImageError("package catalog is empty")
    return tuple(names)


def build_image(
    base_path: Path,
    print_path: Path,
    basic_path: Path,
    catalog_path: Path,
    output_path: Path,
) -> str:
    base = base_path.read_bytes()
    print_module = print_path.read_bytes()
    basic_module = basic_path.read_bytes()
    catalog = catalog_path.read_bytes()
    if len(base) != IMAGE_SIZE:
        raise ImageError(
            f"base image is {len(base)} bytes; expected {IMAGE_SIZE}"
        )
    if base[SECTOR_SIZE - 2 : SECTOR_SIZE] != b"\x55\xaa":
        raise ImageError("base image has no BIOS signature")
    if any(base[PRINT_OFFSET:PACKAGE_END]):
        raise ImageError("base image immutable-module region is not blank")
    if any(base[PACKAGE_END:]):
        raise ImageError("base image unused floppy sectors are not blank")

    print_slot, print_checksum = build_module_slot(
        print_module, PRINT_SIZE, PRINT_SIGNATURE, "printer"
    )
    basic_slot, basic_checksum = build_module_slot(
        basic_module, BASIC_SIZE, BASIC_SIGNATURE, "BASIC"
    )
    package_names = validate_package_catalog(catalog)

    image = bytearray(base)
    image[PRINT_OFFSET:PRINT_END] = print_slot
    image[BASIC_OFFSET:BASIC_END] = basic_slot
    image[PACKAGE_OFFSET:PACKAGE_END] = catalog
    output_path.write_bytes(image)
    rendered_packages = ", ".join(name.decode("ascii") for name in package_names)
    return (
        f"image: installed {len(print_module)}-byte printer module "
        f"(CRC-16 {print_checksum:04x}), {len(basic_module)}-byte BASIC module "
        f"(CRC-16 {basic_checksum:04x}), and {len(package_names)} packages "
        f"({rendered_packages}) in {output_path}"
    )


def main() -> int:
    if len(sys.argv) != 6:
        print(
            f"usage: {Path(sys.argv[0]).name} BASE PRINT_MODULE BASIC_MODULE "
            "PACKAGE_CATALOG OUTPUT",
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
