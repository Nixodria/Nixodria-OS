#!/usr/bin/env python3
"""Build Nixodria with rustc/Cargo running entirely in the QEMU guest."""

import argparse
from contextlib import contextmanager
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import urllib.request

from qemu_guest import Guest, command

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build/system"
CACHE = ROOT / ".nixodria/cache"
IMAGE = BUILD / "nixodria.qcow2"
RUNTIME = ROOT / ".nixodria/nixodria-rust.qcow2"


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def source_files() -> list[Path]:
    paths = [ROOT / "Cargo.toml", ROOT / "system.lock.json"]
    if (ROOT / "Cargo.lock").exists():
        paths.append(ROOT / "Cargo.lock")
    for directory, pattern in (("src", "*.rs"), ("apps", "*.rs"), ("guest", "*.sh")):
        paths.extend(sorted((ROOT / directory).rglob(pattern)))
    return paths


def fingerprint() -> str:
    result = hashlib.sha256()
    for path in source_files() + [Path(__file__), ROOT / "tools/qemu_guest.py"]:
        result.update(str(path.relative_to(ROOT)).encode() + b"\0")
        result.update(path.read_bytes())
    return result.hexdigest()


def checked_iso(lock: dict) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / lock["iso_url"].rsplit("/", 1)[1]
    if path.is_symlink():
        raise RuntimeError(f"refusing symlink cache: {path}")
    if path.exists() and digest(path) == lock["iso_sha256"]:
        return path
    temporary = path.with_suffix(".download")
    print("Downloading pinned Alpine installer...", flush=True)
    try:
        with urllib.request.urlopen(lock["iso_url"], timeout=60) as source, temporary.open("wb") as out:
            shutil.copyfileobj(source, out)
        if digest(temporary) != lock["iso_sha256"]:
            raise RuntimeError("installer SHA-256 mismatch")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


@contextmanager
def serve_sources():
    # Only this explicit source bundle is exposed to the VM, never the checkout
    # or the user's runtime disk. QEMU user networking maps 10.0.2.2 to loopback.
    with tempfile.TemporaryDirectory(prefix="nixodria-source-") as directory:
        archive = Path(directory) / "source.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for path in source_files():
                bundle.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
            functools.partial(Handler, directory=directory))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://10.0.2.2:{server.server_port}/source.tar.gz", digest(archive)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


def install_sources(guest: Guest):
    lock = json.loads((ROOT / "system.lock.json").read_text())
    packages = " ".join(shlex.quote(package) for package in
                        [lock["rust_package"], lock["cargo_package"], *lock["system_packages"]])
    # An unchanged system updates offline. If the locked package set changed,
    # obtain and verify the missing packages inside the guest itself.
    guest.run(f"apk add --no-network --no-progress {packages} || "
              f"apk add --no-cache --no-progress {packages}")
    with serve_sources() as (url, checksum):
        guest.run(f"wget -q -O /tmp/nixodria-source.tar.gz '{url}' && "
                  f"echo '{checksum}  /tmp/nixodria-source.tar.gz' | sha256sum -c - && "
                  "mkdir -p /usr/src/nixodria && "
                  "rm -rf /usr/src/nixodria/src /usr/src/nixodria/apps /usr/src/nixodria/guest && "
                  "tar -xzf /tmp/nixodria-source.tar.gz -C /usr/src/nixodria && "
                  "sh /usr/src/nixodria/guest/install.sh", timeout=1200)


def create_toolchain(lock: dict) -> Path:
    iso = checked_iso(lock)
    identity = hashlib.sha256(json.dumps(lock, sort_keys=True).encode()).hexdigest()[:16]
    image = CACHE / f"toolchain-{identity}.qcow2"
    if image.is_symlink():
        raise RuntimeError("refusing symlink toolchain image")
    if image.exists():
        return image
    temporary = CACHE / f"toolchain-{identity}.partial.qcow2"
    if temporary.exists():
        raise RuntimeError(f"unfinished build exists: {temporary}; move it aside before retrying")
    subprocess.run([os.environ.get("QEMU_IMG", "qemu-img"), "create", "-f", "qcow2",
                    str(temporary), "8G"], check=True)
    os.chmod(temporary, 0o600)
    print("Installing Linux and the full Rust toolchain into a new guest disk...", flush=True)
    try:
        with Guest(temporary, BUILD / "bootstrap.log", iso=iso) as guest:
            guest.login()
            branch = lock["repository_branch"]
            packages = " ".join(shlex.quote(package) for package in
                                [lock["rust_package"], lock["cargo_package"], *lock["system_packages"]])
            guest.run("ip link set eth0 up && udhcpc -i eth0 -q && "
                f"printf 'https://dl-cdn.alpinelinux.org/alpine/{branch}/main\\n"
                f"https://dl-cdn.alpinelinux.org/alpine/{branch}/community\\n' > /etc/apk/repositories && "
                "echo nixodria > /etc/hostname && "
                "printf 'auto lo\\niface lo inet loopback\\nauto eth0\\niface eth0 inet dhcp\\n' > /etc/network/interfaces && "
                "rc-update add networking boot && "
                f"printf '%s\\n' {packages} >> /etc/apk/world && "
                "ERASE_DISKS=/dev/vda KERNELOPTS='console=ttyS0,115200 quiet' "
                "setup-disk -m sys -s 0 -k virt /dev/vda", timeout=1800)
            guest.shutdown()
        temporary.replace(image)
    except BaseException:
        # A partial image can help diagnose failure, but is never selected as a
        # bootable cache or allowed to replace a previous successful build.
        raise
    return image


def build():
    BUILD.mkdir(parents=True, exist_ok=True)
    stamp = BUILD / "source.sha256"
    wanted = fingerprint()
    if IMAGE.exists() and stamp.exists() and stamp.read_text().strip() == wanted:
        print(f"system image is current: {IMAGE}")
        return
    lock = json.loads((ROOT / "system.lock.json").read_text())
    base = create_toolchain(lock)
    temporary = BUILD / "nixodria.partial.qcow2"
    if temporary.exists():
        raise RuntimeError(f"unfinished build exists: {temporary}; move it aside before retrying")
    shutil.copyfile(base, temporary)
    os.chmod(temporary, 0o600)
    print("Building and testing the Rust shell inside Nixodria...", flush=True)
    with Guest(temporary, BUILD / "build.log") as guest:
        guest.login()
        install_sources(guest)
        guest.shutdown()
    subprocess.run([os.environ.get("QEMU_IMG", "qemu-img"), "check", str(temporary)], check=True)
    temporary.replace(IMAGE)
    stamp.write_text(wanted + "\n")
    print(f"built {IMAGE}")


def prepare_runtime():
    if RUNTIME.is_symlink():
        raise RuntimeError(f"refusing symlink runtime: {RUNTIME}")
    if RUNTIME.exists():
        return
    if not IMAGE.is_file():
        raise RuntimeError("run make first to build the system image")
    RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    # Publish only a complete copy. link is exclusive, so concurrent launchers
    # cannot replace an existing runtime, even if another wins during copying.
    fd, temporary_name = tempfile.mkstemp(prefix=".nixodria-rust-", dir=RUNTIME.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as destination, IMAGE.open("rb") as source:
            os.fchmod(destination.fileno(), 0o600)
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
        try:
            os.link(temporary, RUNTIME)
        except FileExistsError:
            if RUNTIME.is_symlink():
                raise RuntimeError(f"refusing symlink runtime: {RUNTIME}")
    finally:
        temporary.unlink(missing_ok=True)
    print(f"created persistent runtime: {RUNTIME}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "run", "update", "runtime"), nargs="?", default="build")
    args = parser.parse_args()
    try:
        if args.action == "build":
            build()
        else:
            prepare_runtime()
            if args.action == "run":
                os.execvp(command(RUNTIME)[0], command(RUNTIME))
            elif args.action == "update":
                with Guest(RUNTIME, BUILD / "update.log") as guest:
                    guest.expect(rb"nix> ")
                    guest.send("sh\n")
                    guest.expect(rb"# ")
                    guest.run("stty -echo")
                    install_sources(guest)
                    guest.shutdown()
                print("updated shell and bundled sources; workspace files preserved")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"system: {error}\n")


if __name__ == "__main__":
    main()
