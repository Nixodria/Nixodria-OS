#!/usr/bin/env python3
"""Prove full Rust compilation, Cargo, editing and persistence inside the guest."""

from pathlib import Path
import re
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from qemu_guest import Guest


def shell(guest: Guest, line: str, expected: bytes = b"") -> bytes:
    guest.send(line + "\n")
    output = guest.expect(rb"nix> ", 180)
    if expected not in output:
        raise RuntimeError(f"missing {expected!r} after {line!r}: {output!r}")
    return output


def main():
    source = Path(sys.argv[1]).resolve()
    logs = source.parent
    with tempfile.TemporaryDirectory(prefix="nixodria-rust-smoke-") as directory:
        image = Path(directory) / "guest.qcow2"
        shutil.copyfile(source, image)
        with Guest(image, logs / "smoke-first.log", network=False) as guest:
            guest.expect(rb"nix> ")
            shell(guest, "stty -echo")
            shell(guest, "rustc --version", b"rustc 1.96.1")
            shell(guest, "cargo --version", b"cargo 1.96.1")
            shell(guest, "pkg list", b"HELLO.rs")
            shell(guest, "pkg install HELLO.rs")
            shell(guest, "run HELLO.rs", b"HELLO FROM NIXODRIA")
            shell(guest, "pkg install TETRIS.rs")
            guest.send("run TETRIS.rs\n")
            guest.expect(rb"a d w s space q", 180)
            guest.send(b"adw \x1b")
            guest.expect(rb"nix> ")

            # Exercise the real guest editor, then compile its newly saved file.
            guest.send("edit EDITED.rs\n")
            guest.expect(rb"/root/workspace/EDITED.rs 1/1")
            guest.send("i")
            guest.expect(rb"I /root/workspace/EDITED.rs")
            guest.send('fn main() { println!("EDITED_INSIDE_NIXODRIA"); }\n')
            # A standalone Escape changes vi mode. A burst containing Escape
            # plus :wq can be interpreted as an unknown terminal key sequence.
            guest.send(b"\x1b")
            guest.expect(rb"- /root/workspace/EDITED.rs")
            guest.send(":wq\n")
            guest.expect(rb"nix> ")
            shell(guest, "run EDITED.rs", b"EDITED_INSIDE_NIXODRIA")

            shell(guest, "sh -c 'echo invalid_rust > INVALID.rs'")
            shell(guest, "run INVALID.rs", b"error")
            shell(guest, "echo SHELL_SURVIVED_ERROR", b"SHELL_SURVIVED_ERROR")
            shell(guest, "sh -c 'printf \"fn main() { println!(\\\"INTERRUPT_READY\\\"); loop { std::thread::sleep(std::time::Duration::from_secs(1)); } }\" > LOOP.rs'")
            guest.send("run LOOP.rs\n")
            guest.expect(rb"INTERRUPT_READY", 180)
            guest.send(b"\x03")
            guest.expect(rb"nix> ")
            shell(guest, "echo SHELL_SURVIVED_INTERRUPT", b"SHELL_SURVIVED_INTERRUPT")

            guest.send("sh\n")
            guest.expect(rb"# ")
            guest.run("sh /usr/lib/nixodria/selftest.sh", timeout=900)
            guest.run("cargo new --vcs none persistent_project && "
                      "echo 'fn main() { println!(\"PERSISTENT_CARGO_OK\"); }' > persistent_project/src/main.rs && "
                      "cd persistent_project && cargo run --offline && cd .. && sync")
            guest.shutdown()

        # A new emulator process, without networking or an installer ISO, must
        # boot and compile the edited sources from its own persistent filesystem.
        with Guest(image, logs / "smoke-restart.log", network=False) as guest:
            guest.expect(rb"nix> ")
            shell(guest, "run EDITED.rs", b"EDITED_INSIDE_NIXODRIA")
            shell(guest, "cd persistent_project")
            shell(guest, "cargo run --offline", b"PERSISTENT_CARGO_OK")
            guest.send("poweroff\n")
            guest.expect(rb"Power down|Powering off", 90)
            guest.process.wait(timeout=30)
    print("smoke: guest Rust/std, Cargo, vi edits, compiler errors, Ctrl-C and restart persistence passed")


if __name__ == "__main__":
    main()
