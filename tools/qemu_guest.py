"""Serial QEMU transport shared by the system builder and integration tests."""

import os
from pathlib import Path
import re
import selectors
import subprocess
import time


def command(image: Path, *, iso: Path | None = None, network: bool = True) -> list[str]:
    args = [os.environ.get("QEMU", "qemu-system-x86_64"), "-accel", "tcg",
            "-m", os.environ.get("QEMU_MEMORY", "2048"), "-smp", "2",
            "-drive", f"file={image.resolve()},format=qcow2,if=virtio",
            "-display", "none", "-chardev", "stdio,id=console,signal=off",
            "-serial", "chardev:console", "-monitor", "none"]
    if iso:
        args += ["-cdrom", str(iso.resolve()), "-boot", "d"]
    if network:
        args += ["-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0"]
    else:
        args += ["-nic", "none"]
    return args


class Guest:
    def __init__(self, image: Path, log: Path, *, iso: Path | None = None,
                 network: bool = True):
        self.process = subprocess.Popen(command(image, iso=iso, network=network),
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        log.parent.mkdir(parents=True, exist_ok=True)
        self.log = log.open("wb")
        self.output = bytearray()
        self.cursor = 0
        self.eof = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.selector.close()
        self.log.close()
        self.process.stdin.close()
        self.process.stdout.close()

    def send(self, value: str | bytes):
        if isinstance(value, str):
            value = value.encode()
        self.process.stdin.write(value)
        self.process.stdin.flush()

    def expect(self, pattern: bytes, timeout: float = 180) -> bytes:
        deadline = time.monotonic() + timeout
        progress = time.monotonic() + 30
        while True:
            match = re.search(pattern, self.output[self.cursor:])
            if match:
                end = self.cursor + match.end()
                result = bytes(self.output[self.cursor:end])
                self.cursor = end
                return result
            if time.monotonic() >= deadline or self.eof:
                tail = bytes(self.output[-3000:]).decode(errors="replace")
                raise RuntimeError(f"guest did not produce {pattern!r}:\n{tail}")
            for key, _ in self.selector.select(1):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    self.eof = True
                    self.selector.unregister(key.fileobj)
                self.output.extend(data)
                self.log.write(data)
                self.log.flush()
            if time.monotonic() > progress:
                print("guest: still working; " + bytes(self.output[-160:]).decode(
                    errors="replace").replace("\r", " ").replace("\n", " "), flush=True)
                progress = time.monotonic() + 30

    def login(self):
        self.expect(rb"login: ")
        self.send("root\n")
        self.expect(rb"[~#]# |:~# ")
        self.send("stty -echo; export PS1='NIXBUILD# '\n")
        self.expect(rb"\r?\nNIXBUILD# ")

    def run(self, script: str, timeout: float = 900) -> bytes:
        self.send(script + "; status=$?; printf '\\nNIXDONE:%s\\n' \"$status\"\n")
        output = self.expect(rb"\r?\nNIXDONE:[0-9]+\r?\n", timeout)
        status = re.search(rb"NIXDONE:([0-9]+)", output).group(1)
        if status != b"0":
            raise RuntimeError("guest command failed:\n" + output.decode(errors="replace"))
        return output

    def shutdown(self):
        self.send("poweroff\n")
        try:
            self.expect(rb"Power down|Powering off", 90)
        except RuntimeError:
            # The installer ISO has a serial login but may send kernel shutdown
            # messages only to VGA. A clean QEMU exit after poweroff is success.
            if not self.eof or self.process.wait(timeout=10) != 0:
                raise
            return
        if self.process.wait(timeout=30) != 0:
            raise RuntimeError("QEMU exited unsuccessfully during shutdown")
