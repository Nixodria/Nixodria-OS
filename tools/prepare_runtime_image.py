#!/usr/bin/env python3
"""Refresh immutable OS/package regions and preserve saved file snapshots."""

import os
from pathlib import Path
import sys
import tempfile


SECTOR_SIZE = 512
IMAGE_SECTORS = 2880
SYSTEM_SECTORS = 11
SNAPSHOT_SECTORS = 33
STORAGE_SECTORS = SNAPSHOT_SECTORS * 2
LEGACY_SYSTEM_SECTORS = (8, 4)
LEGACY_STORAGE_SECTORS = 10
LEGACY_SLOT_SECTORS = 5
IMAGE_SIZE = SECTOR_SIZE * IMAGE_SECTORS
SYSTEM_SIZE = SECTOR_SIZE * SYSTEM_SECTORS
SNAPSHOT_SIZE = SECTOR_SIZE * SNAPSHOT_SECTORS
STORAGE_SIZE = SECTOR_SIZE * STORAGE_SECTORS
STORAGE_OFFSET = SYSTEM_SIZE
STORAGE_END = STORAGE_OFFSET + STORAGE_SIZE
LEGACY_STORAGE_SIZE = SECTOR_SIZE * LEGACY_STORAGE_SECTORS
LEGACY_SLOT_SIZE = SECTOR_SIZE * LEGACY_SLOT_SECTORS
LEGACY_IMAGE_SIZES = tuple(
    SECTOR_SIZE * (system_sectors + LEGACY_STORAGE_SECTORS)
    for system_sectors in LEGACY_SYSTEM_SECTORS
)


class ImageError(RuntimeError):
    pass


def read_image(path: Path, label: str) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ImageError(f"cannot read {label} {path}: {error}") from error
    if len(data) != IMAGE_SIZE:
        raise ImageError(
            f"{label} {path} is {len(data)} bytes; expected {IMAGE_SIZE}"
        )
    if data[SECTOR_SIZE - 2 : SECTOR_SIZE] != b"\x55\xaa":
        raise ImageError(f"{label} {path} has no BIOS signature")
    return data


def read_runtime_image(path: Path) -> tuple[bytes, bool]:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ImageError(f"cannot read runtime image {path}: {error}") from error

    if len(data) != IMAGE_SIZE and len(data) not in LEGACY_IMAGE_SIZES:
        legacy_sizes = " or ".join(str(size) for size in LEGACY_IMAGE_SIZES)
        raise ImageError(
            f"runtime image {path} is {len(data)} bytes; expected {IMAGE_SIZE} "
            f"or legacy {legacy_sizes}"
        )
    if data[SECTOR_SIZE - 2 : SECTOR_SIZE] != b"\x55\xaa":
        raise ImageError(f"runtime image {path} has no BIOS signature")

    return data, len(data) in LEGACY_IMAGE_SIZES


def replace_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", dir=path.parent, delete=False
        ) as temporary:
            temporary_name = temporary.name
            os.chmod(temporary_name, 0o600)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def prepare_runtime_image(template: Path, runtime: Path) -> str:
    template_data = read_image(template, "template image")
    if runtime.is_symlink():
        raise ImageError(f"runtime image must not be a symbolic link: {runtime}")

    runtime_data: bytes | None = None
    if runtime.exists():
        runtime_data, legacy = read_runtime_image(runtime)
        combined_bytes = bytearray(template_data)
        if legacy:
            legacy_storage = runtime_data[-LEGACY_STORAGE_SIZE:]
            combined_bytes[
                STORAGE_OFFSET : STORAGE_OFFSET + LEGACY_SLOT_SIZE
            ] = legacy_storage[:LEGACY_SLOT_SIZE]
            combined_bytes[
                STORAGE_OFFSET + SNAPSHOT_SIZE :
                STORAGE_OFFSET + SNAPSHOT_SIZE + LEGACY_SLOT_SIZE
            ] = legacy_storage[LEGACY_SLOT_SIZE:]
            action = "migrated legacy image and preserved saved files in"
        else:
            combined_bytes[STORAGE_OFFSET:STORAGE_END] = runtime_data[
                STORAGE_OFFSET:STORAGE_END
            ]
            action = "updated immutable sectors and preserved saved files in"
        combined = bytes(combined_bytes)
    else:
        combined = template_data
        action = "created"

    if runtime_data is None or combined != runtime_data:
        replace_atomically(runtime, combined)
    else:
        os.chmod(runtime, 0o600)
    return f"runtime: {action} {runtime}"


def main() -> int:
    if len(sys.argv) != 3:
        print(
            f"usage: {Path(sys.argv[0]).name} TEMPLATE RUNTIME", file=sys.stderr
        )
        return 2

    try:
        message = prepare_runtime_image(Path(sys.argv[1]), Path(sys.argv[2]))
    except ImageError as error:
        print(f"runtime: {error}", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
