#!/usr/bin/env python3
"""Check the pinned package catalog and its fail-closed fetch path."""

from collections.abc import Callable
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import ModuleType


class CheckFailure(RuntimeError):
    pass


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CheckFailure(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, data: bytes, url: str):
        self.data = data
        self.url = url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def geturl(self) -> str:
        return self.url

    def read(self, limit: int) -> bytes:
        return self.data[:limit]


def expect_error(
    error_type: type[BaseException], action: Callable[[], object], label: str
) -> None:
    try:
        action()
    except error_type:
        return
    raise CheckFailure(f"{label} was accepted")


def write_lock(path: Path, digest: str) -> None:
    path.write_text(
        json.dumps(
            {
                "repository": "Nixodria/Nixodria-Packages",
                "tag": "v1.0.1",
                "asset": "nixodria-packages.bin",
                "sha256": digest,
            }
        ),
        encoding="utf-8",
    )


def main() -> int:
    if len(sys.argv) != 6 or sys.argv[1] not in {"pinned", "override"}:
        print(
            f"usage: {Path(sys.argv[0]).name} pinned|override "
            "LOCK CATALOG FETCH_TOOL BUILD_TOOL",
            file=sys.stderr,
        )
        return 2

    mode = sys.argv[1]
    lock_path = Path(sys.argv[2]).resolve()
    catalog_path = Path(sys.argv[3]).resolve()
    fetch = load_module(
        "nixodria_fetch_package_catalog", Path(sys.argv[4]).resolve()
    )
    image = load_module("nixodria_build_image", Path(sys.argv[5]).resolve())

    try:
        catalog = catalog_path.read_bytes()
        url, digest = fetch.read_lock(lock_path)
        expected_url = (
            "https://github.com/Nixodria/Nixodria-Packages/releases/download/"
            "v1.0.1/nixodria-packages.bin"
        )
        actual_digest = hashlib.sha256(catalog).hexdigest()
        if mode == "pinned":
            repository_prefix = (
                "https://github.com/Nixodria/Nixodria-Packages/releases/download/"
            )
            if not url.startswith(repository_prefix) or not url.endswith(
                "/nixodria-packages.bin"
            ):
                raise CheckFailure(f"lock resolves to unexpected URL {url}")
            if digest != actual_digest:
                raise CheckFailure(
                    f"catalog digest is {actual_digest}; lock pins {digest}"
                )

        names = image.validate_package_catalog(catalog)
        required_names = {b"TETRIS.BAS", b"HELLO.BAS"}
        if not required_names.issubset(names):
            raise CheckFailure(f"required package is missing from {names!r}")

        bad_header = bytearray(catalog)
        bad_header[image.PACKAGE_HEADER_CHECKSUM_OFFSET] ^= 0x01
        expect_error(
            image.ImageError,
            lambda: image.validate_package_catalog(bytes(bad_header)),
            "catalog with corrupt header",
        )
        bad_payload = bytearray(catalog)
        bad_payload[image.SECTOR_SIZE] ^= 0x01
        expect_error(
            image.ImageError,
            lambda: image.validate_package_catalog(bytes(bad_payload)),
            "catalog with corrupt payload",
        )

        with tempfile.TemporaryDirectory(prefix="nixodria-catalog-check-") as name:
            root = Path(name)
            test_lock = root / "packages.lock.json"
            write_lock(test_lock, actual_digest)

            cached = root / "cached.bin"
            cached.write_bytes(catalog)
            os.chmod(cached, 0o644)
            original_urlopen = fetch.urlopen
            fetch.urlopen = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("verified cache unexpectedly used the network")
            )
            try:
                message = fetch.fetch_catalog(test_lock, cached)
            finally:
                fetch.urlopen = original_urlopen
            if "using verified catalog" not in message:
                raise CheckFailure("verified cache did not take the cache path")
            if stat.S_IMODE(cached.stat().st_mode) != 0o600:
                raise CheckFailure("verified cache permissions were not restricted")

            downloaded = root / "downloaded.bin"
            fetch.urlopen = lambda *_args, **_kwargs: FakeResponse(
                catalog, expected_url
            )
            try:
                message = fetch.fetch_catalog(test_lock, downloaded)
            finally:
                fetch.urlopen = original_urlopen
            if (
                downloaded.read_bytes() != catalog
                or "downloaded verified" not in message
            ):
                raise CheckFailure("verified download was not installed atomically")
            if stat.S_IMODE(downloaded.stat().st_mode) != 0o600:
                raise CheckFailure("downloaded catalog permissions were not restricted")

            stale = root / "stale.bin"
            stale.write_bytes(b"stale cache")
            stale_before = stale.read_bytes()
            fetch.urlopen = lambda *_args, **_kwargs: FakeResponse(
                bytes(len(catalog)), expected_url
            )
            try:
                expect_error(
                    fetch.CatalogError,
                    lambda: fetch.fetch_catalog(test_lock, stale),
                    "download with the wrong digest",
                )
            finally:
                fetch.urlopen = original_urlopen
            if stale.read_bytes() != stale_before:
                raise CheckFailure("failed download replaced the existing cache")

            target = root / "target.bin"
            target.write_bytes(catalog)
            link = root / "catalog-link.bin"
            link.symlink_to(target)
            expect_error(
                fetch.CatalogError,
                lambda: fetch.fetch_catalog(test_lock, link),
                "symbolic-link cache",
            )

            unsafe_lock = root / "unsafe.lock.json"
            write_lock(unsafe_lock, actual_digest.upper())
            expect_error(
                fetch.CatalogError,
                lambda: fetch.read_lock(unsafe_lock),
                "noncanonical digest",
            )

            extra_key_lock = root / "extra-key.lock.json"
            extra_key_document = json.loads(test_lock.read_text(encoding="utf-8"))
            extra_key_document["url"] = expected_url
            extra_key_lock.write_text(
                json.dumps(extra_key_document), encoding="utf-8"
            )
            expect_error(
                fetch.CatalogError,
                lambda: fetch.read_lock(extra_key_lock),
                "lock with an unrecognized field",
            )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"packages-check: {error}", file=sys.stderr)
        return 1

    rendered = ", ".join(name.decode("ascii") for name in names)
    print(f"packages-check: {mode} catalog and {rendered} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
