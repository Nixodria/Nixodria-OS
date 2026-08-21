#!/usr/bin/env python3
"""Refresh boot code and migrate or preserve Nixodria's document sectors."""

import os
from pathlib import Path
import sys
import tempfile


SECTOR_SIZE = 512
SYSTEM_SECTORS = 8
STORAGE_SECTORS = 10
LEGACY_SYSTEM_SECTORS = 4
SYSTEM_SIZE = SECTOR_SIZE * SYSTEM_SECTORS
STORAGE_SIZE = SECTOR_SIZE * STORAGE_SECTORS
IMAGE_SIZE = SECTOR_SIZE * (SYSTEM_SECTORS + STORAGE_SECTORS)
LEGACY_IMAGE_SIZE = SECTOR_SIZE * (LEGACY_SYSTEM_SECTORS + STORAGE_SECTORS)


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

    if len(data) not in (IMAGE_SIZE, LEGACY_IMAGE_SIZE):
        raise ImageError(
            f"runtime image {path} is {len(data)} bytes; expected {IMAGE_SIZE} "
            f"or legacy {LEGACY_IMAGE_SIZE}"
        )
    if data[SECTOR_SIZE - 2 : SECTOR_SIZE] != b"\x55\xaa":
        raise ImageError(f"runtime image {path} has no BIOS signature")

    return data, len(data) == LEGACY_IMAGE_SIZE


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
        combined = template_data[:SYSTEM_SIZE] + runtime_data[-STORAGE_SIZE:]
        if legacy:
            action = "migrated legacy image and preserved saved text in"
        else:
            action = "updated system sectors and preserved saved text in"
    else:
        combined = template_data
        action = "created"

    if runtime_data is None or combined != runtime_data:
        replace_atomically(runtime, combined)
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
