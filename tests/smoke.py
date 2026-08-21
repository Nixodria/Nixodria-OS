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


CLEAR_SCREEN = b"\x1b[2J\x1b[H"
EDITOR_FRAME = (
    CLEAR_SCREEN
    + b"Nixodria Editor\r\n"
    + b"Ctrl-X exit | Ctrl-L clear\r\n\r\n"
)


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
            if process.poll() is not None:
                raise SmokeFailure("QEMU exited instead of remaining halted")
            events = selector.select(max(0, deadline - time.monotonic()))
            if not events:
                continue
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                raise SmokeFailure("QEMU closed its output instead of remaining halted")
            transcript.extend(chunk)
        if len(transcript) != start:
            raise SmokeFailure("guest produced output after halt")
        if process.poll() is not None:
            raise SmokeFailure("QEMU exited instead of remaining halted")

    def assert_editor_document(
        expected: bytes, start: int, enter: bytes = b"\r"
    ) -> int:
        process.stdin.write(b"edit" + enter)
        process.stdin.flush()
        body_start = wait_for(b"edit\r\n" + EDITOR_FRAME, start)
        process.stdin.write(b"\x18")
        process.stdin.flush()
        end = wait_for(b"\r\nnix> ", body_start)
        actual = bytes(transcript[body_start:end])
        wanted = expected + b"\r\nnix> "
        if actual != wanted:
            raise SmokeFailure(
                f"editor document mismatch: expected {wanted!r}, found {actual!r}"
            )
        return end

    try:
        cursor = wait_for(b"nix> ", 0)

        process.stdin.write(b"help\r\n")
        process.stdin.flush()
        cursor = wait_for(
            b"help\r\nhelp edit clear echo <text> reboot halt\r\nnix> ", cursor
        )

        process.stdin.write(b"echo tinx\by\r")
        process.stdin.flush()
        cursor = wait_for(b"echo tinx\b \by\r\ntiny\r\nnix> ", cursor)

        process.stdin.write(b"nope\r")
        process.stdin.flush()
        cursor = wait_for(b"nope\r\nUnknown command.\r\nnix> ", cursor)

        # Enter with CRLF, then prove its LF half does not become an editor
        # newline. Empty-buffer Backspace and Delete must also be no-ops.
        process.stdin.write(b"edit\r\n")
        process.stdin.flush()
        cursor = wait_for(b"edit\r\n" + EDITOR_FRAME, cursor)
        process.stdin.write(b"\x08\x7falpha\r\nbetx\x08y\x18")
        process.stdin.flush()
        cursor = wait_for(
            b"alpha\r\nbetx"
            + EDITOR_FRAME
            + b"alpha\r\nbety\r\nnix> ",
            cursor,
        )

        # A shell command must not overwrite the separate editor buffer.
        process.stdin.write(b"help\r")
        process.stdin.flush()
        cursor = wait_for(
            b"help\r\nhelp edit clear echo <text> reboot halt\r\nnix> ", cursor
        )
        cursor = assert_editor_document(b"alpha\r\nbety", cursor, b"\r\n")

        # Four character deletions followed by one newline deletion should
        # join the two lines. Every deletion redraws to handle terminal wrap.
        process.stdin.write(b"edit\r")
        process.stdin.flush()
        cursor = wait_for(b"edit\r\n" + EDITOR_FRAME + b"alpha\r\nbety", cursor)
        process.stdin.write(b"\x08\x08\x08\x08\x08Z\x18")
        process.stdin.flush()
        cursor = wait_for(EDITOR_FRAME + b"alphaZ\r\nnix> ", cursor)
        cursor = assert_editor_document(b"alphaZ", cursor, b"\n")

        # Ctrl-L clears the document. CR, LF, and CRLF each create exactly one
        # stored newline, and the document survives a full shell input line.
        process.stdin.write(b"edit\r")
        process.stdin.flush()
        cursor = wait_for(b"edit\r\n" + EDITOR_FRAME + b"alphaZ", cursor)
        process.stdin.write(b"\x0ca\rb\nc\r\nd\x18")
        process.stdin.flush()
        cursor = wait_for(
            EDITOR_FRAME + b"a\r\nb\r\nc\r\nd\r\nnix> ", cursor
        )

        long_unknown = b"x" * 31
        process.stdin.write(long_unknown + b"\r")
        process.stdin.flush()
        cursor = wait_for(
            long_unknown + b"\r\nUnknown command.\r\nnix> ", cursor
        )
        cursor = assert_editor_document(b"a\r\nb\r\nc\r\nd", cursor, b"\r\n")

        # The 2 KiB allocation holds 2047 content bytes plus its terminator.
        # Excess input rings the terminal bell but control keys remain usable.
        process.stdin.write(b"edit\r\x0c")
        process.stdin.flush()
        cursor = wait_for(b"edit\r\n" + EDITOR_FRAME, cursor)
        process.stdin.write(b"A" * 2048 + b"B\x08C\x18")
        process.stdin.flush()
        full_document = b"A" * 2046 + b"C"
        cursor = wait_for(
            b"\x07\x07" + EDITOR_FRAME + full_document + b"\r\nnix> ",
            cursor,
        )

        cursor = assert_editor_document(full_document, cursor, b"\r\n")

        process.stdin.write(b"clear\r")
        process.stdin.flush()
        cursor = wait_for(b"clear\r\n\x1b[2J\x1b[Hnix> ", cursor)

        process.stdin.write(b"reboot\r")
        process.stdin.flush()
        cursor = wait_for(b"reboot\r\nRebooting...\r\n", cursor)
        cursor = wait_for(b"Nixodria OS\r\nType help.\r\nnix> ", cursor)

        # Reboot reinitializes the volatile scratchpad even if RAM itself was
        # not cleared by the BIOS.
        cursor = assert_editor_document(b"", cursor, b"\r\n")

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

    print("smoke: booted and exercised every command, the text editor, and errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
