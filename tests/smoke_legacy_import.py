#!/usr/bin/env python3
"""Verify legacy imports in disposable QEMU disks, including restart/no-overwrite."""

import hashlib
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from import_legacy_files import import_files
from qemu_guest import Guest
from test_export_legacy_files import image, snapshot


def enter_shell(guest: Guest) -> None:
    guest.expect(rb"nix> ")
    guest.send("sh\n")
    guest.expect(rb"# ")
    guest.send("stty -echo\n")
    guest.expect(rb"# ")


def verify_files(guest: Guest, destination: str,
                 files: tuple[tuple[bytes, bytes], ...]) -> None:
    manifest = "\n".join(f"{hashlib.sha256(content).hexdigest()}  ./{name.decode()}"
                         for name, content in files)
    private_files = " && ".join(
        f'test "$(stat -c %a {shlex.quote("./" + name.decode())})" = 600'
        for name, _ in files
    )
    guest.run(f"cd {shlex.quote(destination)} && "
              f"printf '%s\\n' {shlex.quote(manifest)} | sha256sum -c - && "
              f'test "$(stat -c %a .)" = 700 && {private_files} && '
              f'test "$(find . -type f | wc -l)" -eq {len(files)}')


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} RUST_SYSTEM_QCOW2")
    source = Path(sys.argv[1]).resolve()
    logs = source.parent
    files = ((b"HELLO.BAS", b'10 PRINT "LEGACY HELLO"\r\n20 END\r\n'),
             (b"BYTES.TXT", bytes(range(256))), (b"EMPTY.TXT", b""))
    original = image(snapshot(5, ((b"OLD.BAS", b"old fallback"),)), snapshot(6, files))
    with tempfile.TemporaryDirectory(prefix="nixodria-import-smoke-") as temporary:
        directory = Path(temporary).resolve()
        legacy = directory / "legacy.img"
        runtime = directory / "guest.qcow2"
        legacy.write_bytes(original)
        shutil.copyfile(source, runtime)
        print("legacy smoke: importing synthetic legacy saves into a disposable guest", flush=True)
        destination = import_files(legacy, runtime, log=logs / "smoke-import-first.log")

        # Reboot without networking: verify exact original bytes and save a user edit.
        print("legacy smoke: verifying persisted files and saving a user edit", flush=True)
        edited = b"10 REM user changes after migration\n"
        with Guest(runtime, logs / "smoke-import-restart.log", network=False) as guest:
            enter_shell(guest)
            verify_files(guest, destination, files)
            guest.run(f"printf '%s' {shlex.quote(edited.decode())} > HELLO.BAS && sync")
            guest.shutdown()

        print("legacy smoke: proving repeated import refuses to overwrite the edit", flush=True)
        try:
            import_files(legacy, runtime, log=logs / "smoke-import-refused.log")
        except RuntimeError as error:
            if "Legacy destination already exists" not in str(error):
                raise
        else:
            raise RuntimeError("repeat import unexpectedly accepted an existing destination")

        edited_files = ((b"HELLO.BAS", edited),) + files[1:]
        with Guest(runtime, logs / "smoke-import-final.log", network=False) as guest:
            enter_shell(guest)
            verify_files(guest, destination, edited_files)
            guest.run("test -z \"$(find /root/workspace -maxdepth 1 -name '.legacy-import.*' -print)\"")
            guest.shutdown()
        if legacy.read_bytes() != original:
            raise RuntimeError("legacy source image changed during import")
    print("legacy smoke: byte-exact import, private permissions, restart persistence, "
          "repeat-import refusal, user-edit preservation and unchanged source passed")


if __name__ == "__main__":
    main()
