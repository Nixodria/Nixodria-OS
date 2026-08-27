#!/usr/bin/env python3
"""Prove runtime refreshes preserve Nixodria's mutable file snapshots."""

import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


SECTOR_SIZE = 512
IMAGE_SECTORS = 2880
SYSTEM_SECTORS = 11
SNAPSHOT_SECTORS = 33
STORAGE_SECTORS = SNAPSHOT_SECTORS * 2
PRINT_MODULE_SECTORS = 32
BASIC_MODULE_SECTORS = 16
PACKAGE_SLOT_SECTORS = 5
PACKAGE_SLOTS = 8
LEGACY_SYSTEM_SECTORS = (8, 4)
LEGACY_STORAGE_SECTORS = 10
LEGACY_SLOT_SECTORS = 5
IMAGE_SIZE = SECTOR_SIZE * IMAGE_SECTORS
SYSTEM_SIZE = SECTOR_SIZE * SYSTEM_SECTORS
SNAPSHOT_SIZE = SECTOR_SIZE * SNAPSHOT_SECTORS
STORAGE_SIZE = SECTOR_SIZE * STORAGE_SECTORS
STORAGE_OFFSET = SYSTEM_SIZE
STORAGE_END = STORAGE_OFFSET + STORAGE_SIZE
PRINT_OFFSET = STORAGE_END
BASIC_OFFSET = PRINT_OFFSET + SECTOR_SIZE * PRINT_MODULE_SECTORS
PACKAGE_OFFSET = BASIC_OFFSET + SECTOR_SIZE * BASIC_MODULE_SECTORS
PACKAGE_END = (
    PACKAGE_OFFSET
    + SECTOR_SIZE * PACKAGE_SLOT_SECTORS * PACKAGE_SLOTS
)
LEGACY_STORAGE_SIZE = SECTOR_SIZE * LEGACY_STORAGE_SECTORS
LEGACY_SLOT_SIZE = SECTOR_SIZE * LEGACY_SLOT_SECTORS


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


def assert_preparer_fails(preparer: Path, template: Path, runtime: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(preparer), str(template), str(runtime)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        raise RuntimeError(f"invalid runtime image was accepted: {runtime}")


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
            runtime_data = bytearray(template_data)
            runtime_data[STORAGE_OFFSET:STORAGE_END] = storage
            runtime_data[STORAGE_END + 17] ^= 0xA5
            runtime.write_bytes(runtime_data)
            os.chmod(runtime, 0o600)

            refreshed_template = bytearray(template_data)
            immutable_probes = (
                (SYSTEM_SIZE - 1, 0x5A),
                (PRINT_OFFSET + 17, 0x3C),
                (BASIC_OFFSET + 17, 0x69),
                (PACKAGE_OFFSET + 17, 0x96),
                (PACKAGE_END - SECTOR_SIZE + 17, 0xA5),
                (PACKAGE_END + 17, 0xC3),
            )
            for offset, mask in immutable_probes:
                refreshed_template[offset] ^= mask
            template.write_bytes(refreshed_template)
            run_preparer(preparer, template, runtime)

            result = runtime.read_bytes()
            if template.read_bytes() != bytes(refreshed_template):
                raise RuntimeError("runtime refresh changed its template")
            expected_refresh = bytearray(refreshed_template)
            expected_refresh[STORAGE_OFFSET:STORAGE_END] = storage
            if result != bytes(expected_refresh):
                raise RuntimeError(
                    "runtime refresh did not replace immutable sectors while "
                    "preserving file snapshots"
                )
            if stat.S_IMODE(runtime.stat().st_mode) != 0o600:
                raise RuntimeError("refreshed runtime image is not private mode 0600")

            legacy_storage = bytes(
                (index * 19 + 7) & 0xFF for index in range(LEGACY_STORAGE_SIZE)
            )
            for legacy_system_sectors in LEGACY_SYSTEM_SECTORS:
                legacy = root / f"legacy-{legacy_system_sectors}.img"
                legacy_system_size = SECTOR_SIZE * legacy_system_sectors
                legacy_data = (
                    template_data[:legacy_system_size] + legacy_storage
                )
                expected_size = legacy_system_size + LEGACY_STORAGE_SIZE
                if len(legacy_data) != expected_size:
                    raise RuntimeError("legacy migration fixture has the wrong size")
                legacy.write_bytes(legacy_data)
                os.chmod(legacy, 0o644)
                run_preparer(preparer, template, legacy)

                migrated = legacy.read_bytes()
                expected_migration = bytearray(refreshed_template)
                expected_migration[
                    STORAGE_OFFSET : STORAGE_OFFSET + LEGACY_SLOT_SIZE
                ] = legacy_storage[:LEGACY_SLOT_SIZE]
                expected_migration[
                    STORAGE_OFFSET + SNAPSHOT_SIZE :
                    STORAGE_OFFSET + SNAPSHOT_SIZE + LEGACY_SLOT_SIZE
                ] = legacy_storage[LEGACY_SLOT_SIZE:]
                if migrated != bytes(expected_migration):
                    raise RuntimeError(
                        f"legacy {legacy_system_sectors}-sector-system migration "
                        "did not place both NIX2 records into the NIX3 snapshots"
                    )
                if stat.S_IMODE(legacy.stat().st_mode) != 0o600:
                    raise RuntimeError(
                        "migrated runtime image is not private mode 0600"
                    )

                run_preparer(preparer, template, legacy)
                if legacy.read_bytes() != bytes(expected_migration):
                    raise RuntimeError(
                        "second refresh changed the migrated runtime image"
                    )

            malformed = root / "malformed.img"
            malformed_bytes = b"existing state must not be overwritten"
            malformed.write_bytes(malformed_bytes)
            assert_preparer_fails(preparer, template, malformed)
            if malformed.read_bytes() != malformed_bytes:
                raise RuntimeError("failed refresh overwrote malformed runtime state")

            unsigned_legacy = root / "unsigned-legacy.img"
            unsigned_data = bytes(
                SECTOR_SIZE
                * (LEGACY_SYSTEM_SECTORS[0] + LEGACY_STORAGE_SECTORS)
            )
            unsigned_legacy.write_bytes(unsigned_data)
            assert_preparer_fails(preparer, template, unsigned_legacy)
            if unsigned_legacy.read_bytes() != unsigned_data:
                raise RuntimeError("failed legacy migration changed its runtime image")

            symlink_target = root / "symlink-target.img"
            symlink_target.write_bytes(template_data)
            symlink = root / "runtime-symlink.img"
            symlink.symlink_to(symlink_target)
            assert_preparer_fails(preparer, template, symlink)
            if not symlink.is_symlink() or symlink_target.read_bytes() != template_data:
                raise RuntimeError("failed symlink refresh changed its target")

            missing_target = root / "missing-target.img"
            broken_symlink = root / "broken-runtime-symlink.img"
            broken_symlink.symlink_to(missing_target)
            assert_preparer_fails(preparer, template, broken_symlink)
            if not broken_symlink.is_symlink() or missing_target.exists():
                raise RuntimeError("failed broken-symlink refresh created its target")
            if template.read_bytes() != bytes(refreshed_template):
                raise RuntimeError("runtime checks changed their template")
        if source.read_bytes() != template_data:
            raise RuntimeError("runtime checks changed the source build image")
    except (OSError, RuntimeError) as error:
        print(f"runtime-check: {error}", file=sys.stderr)
        return 1

    print("runtime-check: refresh and NIX2 migration preserved file snapshots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
