#!/usr/bin/env python3
"""Fetch Nixodria's pinned package catalog with an exact digest check."""

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import URLError
from urllib.request import Request, urlopen


CATALOG_SIZE = 8 * 5 * 512
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
TAG_PATTERN = re.compile(r"[A-Za-z0-9_.-]+\Z")
ASSET_PATTERN = re.compile(r"[A-Za-z0-9_.-]+\Z")
LOCK_KEYS = {"repository", "tag", "asset", "sha256"}


class CatalogError(RuntimeError):
    pass


def read_lock(path: Path) -> tuple[str, str]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CatalogError(f"cannot read lock file {path}: {error}") from error
    if not isinstance(lock, dict) or set(lock) != LOCK_KEYS:
        raise CatalogError(
            "lock file must contain exactly repository, tag, asset, and sha256"
        )

    repository = lock.get("repository")
    tag = lock.get("tag")
    asset = lock.get("asset")
    digest = lock.get("sha256")
    if not isinstance(repository, str) or not REPOSITORY_PATTERN.fullmatch(repository):
        raise CatalogError("lock file repository is invalid")
    if not isinstance(tag, str) or not TAG_PATTERN.fullmatch(tag):
        raise CatalogError("lock file tag is invalid")
    if not isinstance(asset, str) or not ASSET_PATTERN.fullmatch(asset):
        raise CatalogError("lock file asset is invalid")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(value not in "0123456789abcdef" for value in digest)
    ):
        raise CatalogError("lock file sha256 is invalid")

    url = f"https://github.com/{repository}/releases/download/{tag}/{asset}"
    return url, digest


def validate_catalog(data: bytes, expected_digest: str) -> None:
    if len(data) != CATALOG_SIZE:
        raise CatalogError(
            f"catalog is {len(data)} bytes; expected exactly {CATALOG_SIZE}"
        )
    actual_digest = hashlib.sha256(data).hexdigest()
    if actual_digest != expected_digest:
        raise CatalogError(
            f"catalog SHA-256 is {actual_digest}; expected {expected_digest}"
        )


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


def fetch_catalog(lock_path: Path, output_path: Path) -> str:
    if output_path.is_symlink():
        raise CatalogError(f"catalog cache must not be a symbolic link: {output_path}")
    url, expected_digest = read_lock(lock_path)

    if output_path.exists():
        try:
            cached = output_path.read_bytes()
            validate_catalog(cached, expected_digest)
        except (OSError, CatalogError):
            pass
        else:
            try:
                os.chmod(output_path, 0o600)
            except OSError as error:
                raise CatalogError(
                    f"cannot secure catalog cache {output_path}: {error}"
                ) from error
            return f"packages: using verified catalog {output_path}"

    request = Request(url, headers={"User-Agent": "Nixodria-OS package fetcher"})
    try:
        with urlopen(request, timeout=60) as response:
            if response.geturl().split(":", 1)[0].lower() != "https":
                raise CatalogError("catalog download left HTTPS")
            data = response.read(CATALOG_SIZE + 1)
    except (OSError, URLError) as error:
        raise CatalogError(f"cannot download {url}: {error}") from error

    validate_catalog(data, expected_digest)
    try:
        replace_atomically(output_path, data)
    except OSError as error:
        raise CatalogError(
            f"cannot replace catalog cache {output_path}: {error}"
        ) from error
    return f"packages: downloaded verified catalog to {output_path}"


def main() -> int:
    if len(sys.argv) != 3:
        print(
            f"usage: {Path(sys.argv[0]).name} LOCK_FILE OUTPUT",
            file=sys.stderr,
        )
        return 2
    try:
        message = fetch_catalog(Path(sys.argv[1]), Path(sys.argv[2]))
    except CatalogError as error:
        print(f"packages: {error}", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
