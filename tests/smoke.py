#!/usr/bin/env python3
"""Boot Nixodria OS in QEMU and exercise its serial command loop."""

import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import time


class SmokeFailure(RuntimeError):
    pass


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} IMAGE", file=sys.stderr)
        return 2

    image = Path(sys.argv[1]).resolve()
    qemu_name = os.environ.get("QEMU", "qemu-system-i386")
    qemu = shutil.which(qemu_name)
    if qemu is None:
        print(f"smoke: QEMU executable not found: {qemu_name}", file=sys.stderr)
        return 1

    command = [
        qemu,
        "-accel",
        "tcg",
        "-boot",
        "a",
        "-drive",
        f"format=raw,file={image},if=floppy",
        "-display",
        "none",
        "-serial",
        "stdio",
        "-monitor",
        "none",
        "-no-reboot",
        "-no-shutdown",
    ]

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    transcript = bytearray()
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    assert process.stdin is not None
    selector.register(process.stdout, selectors.EVENT_READ)

    def wait_for(expected: bytes, start: int, timeout: float = 5.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = transcript.find(expected, start)
            if match >= 0:
                return match + len(expected)
            events = selector.select(max(0, deadline - time.monotonic()))
            if not events:
                continue
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            transcript.extend(chunk)
        raise SmokeFailure(f"timed out waiting for {expected!r}")

    def assert_quiet(start: int, timeout: float = 0.25) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = selector.select(max(0, deadline - time.monotonic()))
            if not events:
                continue
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            transcript.extend(chunk)
        if len(transcript) != start:
            raise SmokeFailure("guest produced output after halt")

    try:
        cursor = wait_for(b"nix> ", 0)

        process.stdin.write(b"help\r\n")
        process.stdin.flush()
        cursor = wait_for(
            b"help\r\nhelp clear echo <text> reboot halt\r\nnix> ", cursor
        )

        process.stdin.write(b"echo tinx\by\r")
        process.stdin.flush()
        cursor = wait_for(b"echo tinx\b \by\r\ntiny\r\nnix> ", cursor)

        process.stdin.write(b"nope\r")
        process.stdin.flush()
        cursor = wait_for(b"nope\r\nUnknown command.\r\nnix> ", cursor)

        process.stdin.write(b"clear\r")
        process.stdin.flush()
        cursor = wait_for(b"clear\r\n\x1b[2J\x1b[Hnix> ", cursor)

        process.stdin.write(b"reboot\r")
        process.stdin.flush()
        cursor = wait_for(b"reboot\r\nRebooting...\r\n", cursor)
        cursor = wait_for(b"Nixodria OS\r\nType help.\r\nnix> ", cursor)

        process.stdin.write(b"halt\r")
        process.stdin.flush()
        cursor = wait_for(b"halt\r\nHalted.\r\n", cursor)
        assert_quiet(cursor)
    except (BrokenPipeError, SmokeFailure) as error:
        rendered = transcript.decode("utf-8", errors="replace")
        print(f"smoke: {error}\n--- transcript ---\n{rendered}", file=sys.stderr)
        return 1
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    print("smoke: booted and exercised every command, editing, and error handling")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
