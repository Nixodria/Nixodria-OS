#!/usr/bin/env python3
"""Copy verified legacy saves into a new directory on a Nixodria Rust disk.

The legacy image is read only. QEMU writes only to the explicitly supplied Rust
disk. BASIC files remain readable source files; this does not translate BASIC.
"""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import http.server
import io
import os
from pathlib import Path
import shlex
import stat
import sys
import tarfile
import tempfile
import threading

from export_legacy_files import ExportError, Snapshot, open_parent, read_legacy_image, recover_files
from qemu_guest import Guest


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ImportBundle:
    archive: Path
    archive_digest: str
    image_digest: str
    snapshot: Snapshot | None

    @property
    def destination(self) -> str:
        return f"/root/workspace/legacy-{self.image_digest[:12]}"

    @property
    def files(self) -> tuple[tuple[bytes, bytes], ...]:
        return self.snapshot.files if self.snapshot else ()


def build_bundle(image: Path, archive: Path) -> ImportBundle:
    """Archive only checksum-verified saved files, with no filesystem metadata."""
    data = read_legacy_image(image)
    snapshot = recover_files(data)
    # Fresh TarInfo records prevent host ownership, symlinks or extra files from
    # entering the guest. The exporter validated each short ASCII basename.
    with archive.open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
            for filename, content in snapshot.files if snapshot else ():
                info = tarfile.TarInfo(filename.decode("ascii"))
                info.size = len(content)
                info.mode = 0o600
                info.mtime = 0
                info.uid = info.gid = 0
                bundle.addfile(info, io.BytesIO(content))
        output.flush()
        os.fsync(output.fileno())
    return ImportBundle(archive, hashlib.sha256(archive.read_bytes()).hexdigest(),
                        hashlib.sha256(data).hexdigest(), snapshot)


@contextmanager
def serve_bundle(bundle: ImportBundle):
    """Expose exactly one temporary archive on loopback, without a directory index."""
    content = bundle.archive.read_bytes()
    if hashlib.sha256(content).hexdigest() != bundle.archive_digest:
        raise ExportError("temporary import archive changed before serving")
    request_path = f"/{bundle.archive_digest}.tar"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != request_path:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-tar")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://10.0.2.2:{server.server_port}{request_path}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def import_script(bundle: ImportBundle, url: str) -> str:
    """Create a fail-closed POSIX shell script containing no user-supplied paths."""
    destination = shlex.quote(bundle.destination)
    checks = ""
    if bundle.files:
        manifest = "\n".join(
            f"{hashlib.sha256(content).hexdigest()}  ./{filename.decode('ascii')}"
            for filename, content in bundle.files
        )
        checks = f"printf '%s\\n' {shlex.quote(manifest)} | (cd \"$stage\" && sha256sum -c -)"
    else:
        checks = ":"
    # The fresh staging directory and final directory are on the same guest
    # filesystem. mv -nT prevents replacement even if a target appears mid-import.
    return f"""set -eu
umask 077
destination={destination}
if [ -e "$destination" ] || [ -L "$destination" ]; then
    echo "Legacy destination already exists; no files were overwritten: $destination" >&2
    exit 1
fi
if [ ! -d /root/workspace ] || [ -L /root ] || [ -L /root/workspace ]; then
    echo 'The guest workspace must be a real directory.' >&2
    exit 1
fi
archive=''
stage=''
cleanup() {{
    if [ -n "$archive" ]; then rm -f -- "$archive"; fi
    if [ -n "$stage" ]; then rm -rf -- "$stage"; fi
}}
trap cleanup EXIT HUP INT TERM
archive=$(mktemp /tmp/nixodria-legacy.XXXXXX)
stage=$(mktemp -d /root/workspace/.legacy-import.XXXXXX)
wget -q -O "$archive" {shlex.quote(url)}
printf '%s  %s\\n' {shlex.quote(bundle.archive_digest)} "$archive" | sha256sum -c -
tar -xf "$archive" -C "$stage"
{checks}
sync
mv -nT -- "$stage" "$destination"
if [ -d "$stage" ]; then
    echo 'Legacy destination appeared during import; no files were overwritten.' >&2
    exit 1
fi
stage=''
sync
printf 'Imported %s legacy files into %s\\n' {len(bundle.files)} "$destination"
"""


def checked_runtime(path: Path) -> Path:
    """Reject symlinks and non-QCOW2 paths before launching a writable guest."""
    parent, name = open_parent(path)
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as image:
            if not stat.S_ISREG(os.fstat(image.fileno()).st_mode):
                raise ExportError("Rust runtime must be a regular file")
            if image.read(4) != b"QFI\xfb":
                raise ExportError("Rust runtime must be an existing QCOW2 image")
    finally:
        os.close(parent)
    absolute = Path(os.path.abspath(path))
    if "," in str(absolute):
        raise ExportError("Rust runtime path cannot contain a comma (QEMU drive option separator)")
    return absolute


def import_files(image: Path, runtime: Path, *, log: Path | None = None) -> str:
    runtime = checked_runtime(runtime)
    with tempfile.TemporaryDirectory(prefix="nixodria-legacy-import-") as directory:
        bundle = build_bundle(image, Path(directory) / "legacy.tar")
        with serve_bundle(bundle) as url, Guest(runtime, log or ROOT / "build/system/import.log") as guest:
            guest.expect(rb"nix> ")
            guest.send("sh\n")
            guest.expect(rb"# ")
            guest.send("stty -echo\n")
            guest.expect(rb"# ")
            try:
                guest.run("sh -c " + shlex.quote(import_script(bundle, url)))
            finally:
                guest.shutdown()
    return bundle.destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="legacy disk to read without changes")
    parser.add_argument("runtime", type=Path, help="existing Nixodria Rust QCOW2 disk to import into")
    args = parser.parse_args()
    try:
        destination = import_files(args.image, args.runtime)
    except (OSError, RuntimeError) as error:
        print(f"legacy import: {error}", file=sys.stderr)
        return 1
    print(f"Legacy files are available inside Nixodria at {destination}.")
    print("The original image is unchanged. BASIC source was preserved without translation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
