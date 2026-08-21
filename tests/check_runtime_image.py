#!/usr/bin/env python3
"""Prove runtime refreshes preserve Nixodria's mutable storage sectors."""

import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


SECTOR_SIZE = 512
SYSTEM_SECTORS = 4
STORAGE_SECTORS = 10
SYSTEM_SIZE = SECTOR_SIZE * SYSTEM_SECTORS
IMAGE_SIZE = SECTOR_SIZE * (SYSTEM_SECTORS + STORAGE_SECTORS)


def run_preparer(preparer: Path, template: Path, runtime: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(preparer), str(template), str(runtime)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"runtime preparer failed: {detail}")


def main() -> int:
    if len(sys.argv) != 3:
        print(
            f"usage: {Path(sys.argv[0]).name} TEMPLATE PREPARER", file=sys.stderr
        )
        return 2

    source = Path(sys.argv[1]).resolve()
    preparer = Path(sys.argv[2]).resolve()
    template_data = source.read_bytes()
    if len(template_data) != IMAGE_SIZE:
        print(
            f"runtime-check: expected {IMAGE_SIZE} template bytes, "
            f"found {len(template_data)}",
            file=sys.stderr,
        )
        return 1

    try:
        with tempfile.TemporaryDirectory(prefix="nixodria-runtime-check-") as directory:
            root = Path(directory)
            template = root / "template.img"
            runtime = root / "runtime.img"
            template.write_bytes(template_data)

            run_preparer(preparer, template, runtime)
            if runtime.read_bytes() != template_data:
                raise RuntimeError("new runtime image does not match its template")
            if template.read_bytes() != template_data:
                raise RuntimeError("runtime creation changed its template")
            if stat.S_IMODE(runtime.stat().st_mode) != 0o600:
                raise RuntimeError("new runtime image is not private mode 0600")

            storage = bytes(
                (index * 37 + 11) & 0xFF
                for index in range(SECTOR_SIZE * STORAGE_SECTORS)
            )
            runtime.write_bytes(template_data[:SYSTEM_SIZE] + storage)
            os.chmod(runtime, 0o600)

            refreshed_template = bytearray(template_data)
            refreshed_template[SYSTEM_SIZE - 1] ^= 0x5A
            template.write_bytes(refreshed_template)
            run_preparer(preparer, template, runtime)

            result = runtime.read_bytes()
            if template.read_bytes() != bytes(refreshed_template):
                raise RuntimeError("runtime refresh changed its template")
            if result[:SYSTEM_SIZE] != refreshed_template[:SYSTEM_SIZE]:
                raise RuntimeError("runtime refresh did not install new system sectors")
            if result[SYSTEM_SIZE:] != storage:
                raise RuntimeError("runtime refresh changed persistent storage")
            if stat.S_IMODE(runtime.stat().st_mode) != 0o600:
                raise RuntimeError("refreshed runtime image is not private mode 0600")

            malformed = root / "malformed.img"
            malformed_bytes = b"existing state must not be overwritten"
            malformed.write_bytes(malformed_bytes)
            failed = subprocess.run(
                [sys.executable, str(preparer), str(template), str(malformed)],
                check=False,
                capture_output=True,
                text=True,
            )
            if failed.returncode == 0:
                raise RuntimeError("malformed runtime image was accepted")
            if malformed.read_bytes() != malformed_bytes:
                raise RuntimeError("failed refresh overwrote malformed runtime state")
        if source.read_bytes() != template_data:
            raise RuntimeError("runtime checks changed the source build image")
    except (OSError, RuntimeError) as error:
        print(f"runtime-check: {error}", file=sys.stderr)
        return 1

    print("runtime-check: system refresh preserved all persistent storage bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
