#!/usr/bin/env python3
"""Boot Nixodria OS in QEMU and exercise its shell and durable editor."""

import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tempfile
import time


class SmokeFailure(RuntimeError):
    pass


SECTOR_SIZE = 512
SYSTEM_SECTORS = 4
SLOT_SECTORS = 5
SLOT_SIZE = SLOT_SECTORS * SECTOR_SIZE
STORAGE_OFFSET = SYSTEM_SECTORS * SECTOR_SIZE
SLOT_HEADER_OFFSETS = (STORAGE_OFFSET, STORAGE_OFFSET + SLOT_SIZE)
SLOT_PAYLOAD_OFFSETS = tuple(offset + SECTOR_SIZE for offset in SLOT_HEADER_OFFSETS)
IMAGE_SIZE = (SYSTEM_SECTORS + 2 * SLOT_SECTORS) * SECTOR_SIZE
STORAGE_MAGIC = b"NIX2"

CLEAR_SCREEN = b"\x1b[2J\x1b[H"
EDITOR_HEADER = (
    CLEAR_SCREEN
    + b"Nixodria Editor\r\n"
    + b"Ctrl-S save | Ctrl-X exit | Ctrl-L clear\r\n"
)
EDITOR_FRAME = EDITOR_HEADER + b"\r\n"
EDITOR_SAVED_FRAME = EDITOR_HEADER + b"Saved.\r\n\r\n"
EDITOR_FAILED_FRAME = EDITOR_HEADER + b"Save failed.\r\n\r\n"


class QemuSession:
    def __init__(
        self,
        qemu: str,
        image: Path,
        *,
        readonly: bool = False,
        blkdebug_config: Path | None = None,
    ):
        filename = str(image)
        if blkdebug_config is not None:
            filename = f"blkdebug:{blkdebug_config}:{image}"
        drive = f"format=raw,file={filename},if=floppy,cache=writethrough"
        if readonly:
            drive += ",readonly=on"
        command = [
            qemu,
            "-accel",
            "tcg",
            "-boot",
            "a",
            "-drive",
            drive,
            "-display",
            "none",
            "-serial",
            "stdio",
            "-monitor",
            "none",
            "-no-reboot",
            "-no-shutdown",
        ]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        self.transcript = bytearray()
        self.selector = selectors.DefaultSelector()
        assert self.process.stdout is not None
        assert self.process.stdin is not None
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def __enter__(self) -> "QemuSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def write(self, data: bytes) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def wait_for(self, expected: bytes, start: int, timeout: float = 5.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = self.transcript.find(expected, start)
            if match >= 0:
                return match + len(expected)
            events = self.selector.select(max(0, deadline - time.monotonic()))
            if not events:
                continue
            assert self.process.stdout is not None
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                break
            self.transcript.extend(chunk)
        status = self.process.poll()
        suffix = "" if status is None else f"; QEMU exited with status {status}"
        rendered = bytes(self.transcript[-4096:]).decode("utf-8", errors="replace")
        raise SmokeFailure(
            f"timed out waiting for {expected!r}{suffix}\n"
            f"--- session transcript tail ---\n{rendered}"
        )

    def assert_quiet(self, start: int, timeout: float = 0.25) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise SmokeFailure("QEMU exited instead of remaining halted")
            events = self.selector.select(max(0, deadline - time.monotonic()))
            if not events:
                continue
            assert self.process.stdout is not None
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                raise SmokeFailure("QEMU closed its output instead of remaining halted")
            self.transcript.extend(chunk)
        if len(self.transcript) != start:
            raise SmokeFailure("guest produced output after halt")
        if self.process.poll() is not None:
            raise SmokeFailure("QEMU exited instead of remaining halted")

    def close(self) -> None:
        self.selector.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)


def assert_editor_document(
    session: QemuSession, expected: bytes, start: int, enter: bytes = b"\r"
) -> int:
    session.write(b"edit" + enter)
    body_start = session.wait_for(b"edit\r\n" + EDITOR_FRAME, start)
    session.write(b"\x18")
    end = session.wait_for(b"\r\nnix> ", body_start)
    actual = bytes(session.transcript[body_start:end])
    wanted = expected + b"\r\nnix> "
    if actual != wanted:
        raise SmokeFailure(
            f"editor document mismatch: expected {wanted!r}, found {actual!r}"
        )
    return end


def halt(session: QemuSession, cursor: int) -> None:
    session.write(b"halt\r")
    cursor = session.wait_for(b"halt\r\nHalted.\r\n", cursor)
    session.assert_quiet(cursor)


def checksum16(data: bytes) -> int:
    checksum = 0xFFFF
    for value in data:
        checksum ^= value << 8
        for _ in range(8):
            if checksum & 0x8000:
                checksum = ((checksum << 1) ^ 0x1021) & 0xFFFF
            else:
                checksum = (checksum << 1) & 0xFFFF
    return checksum


def build_record(generation: int, document: bytes) -> bytes:
    if len(document) > 2047:
        raise ValueError("document exceeds the on-disk format")
    header = bytearray(SECTOR_SIZE)
    header[:4] = STORAGE_MAGIC
    header[4:6] = (generation & 0xFFFF).to_bytes(2, "little")
    header[6:8] = len(document).to_bytes(2, "little")
    header[8:10] = checksum16(document).to_bytes(2, "little")
    header[10:12] = checksum16(header[:10]).to_bytes(2, "little")
    payload = document + bytes(2048 - len(document))
    return bytes(header) + payload


def install_record(image: bytearray, slot: int, record: bytes) -> None:
    if len(record) != SLOT_SIZE:
        raise ValueError("save record has the wrong size")
    start = SLOT_HEADER_OFFSETS[slot]
    image[start : start + SLOT_SIZE] = record


def parse_record(data: bytes, slot: int) -> tuple[int, bytes] | None:
    header_offset = SLOT_HEADER_OFFSETS[slot]
    payload_offset = SLOT_PAYLOAD_OFFSETS[slot]
    header = data[header_offset:payload_offset]
    payload = data[payload_offset : payload_offset + 2048]
    if header[:4] != STORAGE_MAGIC:
        return None
    generation = int.from_bytes(header[4:6], "little")
    length = int.from_bytes(header[6:8], "little")
    if length > 2047:
        return None
    if int.from_bytes(header[10:12], "little") != checksum16(header[:10]):
        return None
    document = payload[:length]
    if int.from_bytes(header[8:10], "little") != checksum16(document):
        return None
    return generation, document


def newest_record(data: bytes) -> tuple[int, int, bytes] | None:
    record_a = parse_record(data, 0)
    record_b = parse_record(data, 1)
    if record_a is None and record_b is None:
        return None
    if record_b is None:
        assert record_a is not None
        generation, document = record_a
        return 0, generation, document
    if record_a is None:
        generation, document = record_b
        return 1, generation, document

    generation_a, document_a = record_a
    generation_b, document_b = record_b
    delta = (generation_a - generation_b) & 0xFFFF
    if delta <= 0x8000:
        return 0, generation_a, document_a
    return 1, generation_b, document_b


def assert_saved_record(
    image: Path,
    expected: bytes,
    system: bytes,
    *,
    expected_slot: int,
    expected_generation: int,
) -> bytes:
    data = image.read_bytes()
    if len(data) != IMAGE_SIZE:
        raise SmokeFailure(f"runtime image changed size to {len(data)} bytes")
    if data[: len(system)] != system:
        raise SmokeFailure("saving text changed boot or kernel sectors")

    newest = newest_record(data)
    wanted = (expected_slot, expected_generation, expected)
    if newest != wanted:
        raise SmokeFailure(f"newest save is {newest!r}; expected {wanted!r}")

    for slot in range(2):
        record = parse_record(data, slot)
        if record is None:
            continue
        _, document = record
        header_offset = SLOT_HEADER_OFFSETS[slot]
        payload_offset = SLOT_PAYLOAD_OFFSETS[slot]
        header = data[header_offset:payload_offset]
        payload = data[payload_offset : payload_offset + 2048]
        if any(header[12:]) or any(payload[len(document) :]):
            raise SmokeFailure(f"slot {slot} retained nonzero unused bytes")
    return data


def exercise_editor_and_save(qemu: str, image: Path) -> bytes:
    full_document = b"A" * 510 + b"\r\n" + b"B" * 512 + b"C" * 512 + b"D" * 511
    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)

        session.write(b"help\r\n")
        cursor = session.wait_for(
            b"help\r\nhelp edit clear echo <text> reboot halt\r\nnix> ", cursor
        )

        session.write(b"echo tinx\by\r")
        cursor = session.wait_for(b"echo tinx\b \by\r\ntiny\r\nnix> ", cursor)

        session.write(b"nope\r")
        cursor = session.wait_for(b"nope\r\nUnknown command.\r\nnix> ", cursor)

        # CRLF creates one editor newline. Empty-buffer Backspace and Delete
        # are no-ops, while Backspace edits the end of a line.
        session.write(b"edit\r\n")
        cursor = session.wait_for(b"edit\r\n" + EDITOR_FRAME, cursor)
        session.write(b"\x08\x7falpha\r\nbetx\x08y\x18")
        cursor = session.wait_for(
            b"alpha\r\nbetx"
            + EDITOR_FRAME
            + b"alpha\r\nbety\r\nnix> ",
            cursor,
        )

        # Shell input cannot overwrite the separate editor buffer.
        session.write(b"help\r")
        cursor = session.wait_for(
            b"help\r\nhelp edit clear echo <text> reboot halt\r\nnix> ", cursor
        )
        cursor = assert_editor_document(session, b"alpha\r\nbety", cursor, b"\r\n")

        # Deleting four characters and one CRLF joins the lines.
        session.write(b"edit\r")
        cursor = session.wait_for(b"edit\r\n" + EDITOR_FRAME + b"alpha\r\nbety", cursor)
        session.write(b"\x08\x08\x08\x08\x08Z\x18")
        cursor = session.wait_for(EDITOR_FRAME + b"alphaZ\r\nnix> ", cursor)
        cursor = assert_editor_document(session, b"alphaZ", cursor, b"\n")

        # CR, LF, and CRLF each create one stored newline.
        session.write(b"edit\r")
        cursor = session.wait_for(b"edit\r\n" + EDITOR_FRAME + b"alphaZ", cursor)
        session.write(b"\x0ca\rb\nc\r\nd\x18")
        cursor = session.wait_for(
            EDITOR_FRAME + b"a\r\nb\r\nc\r\nd\r\nnix> ", cursor
        )

        long_unknown = b"x" * 31
        session.write(long_unknown + b"\r")
        cursor = session.wait_for(
            long_unknown + b"\r\nUnknown command.\r\nnix> ", cursor
        )
        cursor = assert_editor_document(session, b"a\r\nb\r\nc\r\nd", cursor, b"\r\n")

        # Fill all 2,047 content bytes across every payload sector. Excess
        # input rings the bell; deletion and control keys remain usable.
        session.write(b"edit\r\x0c")
        cursor = session.wait_for(b"edit\r\n" + EDITOR_FRAME, cursor)
        session.write(full_document + b"Z")
        cursor = session.wait_for(full_document + b"\x07", cursor)
        session.write(b"\x08D")
        cursor = session.wait_for(EDITOR_FRAME + full_document, cursor)

        session.write(b"\x13")
        cursor = session.wait_for(EDITOR_SAVED_FRAME + full_document, cursor)
        session.write(b"\x18")
        cursor = session.wait_for(b"\r\nnix> ", cursor)

        session.write(b"clear\r")
        cursor = session.wait_for(b"clear\r\n\x1b[2J\x1b[Hnix> ", cursor)

        # A guest BIOS reboot reloads the saved record.
        session.write(b"reboot\r")
        cursor = session.wait_for(b"reboot\r\nRebooting...\r\n", cursor)
        cursor = session.wait_for(b"Nixodria OS\r\nType help.\r\nnix> ", cursor)
        cursor = assert_editor_document(session, full_document, cursor, b"\r\n")

        # Exiting without Ctrl-S leaves the last durable version intact.
        session.write(b"edit\r")
        cursor = session.wait_for(b"edit\r\n" + EDITOR_FRAME + full_document, cursor)
        session.write(b"\x0ctemporary\x18")
        cursor = session.wait_for(EDITOR_FRAME + b"temporary\r\nnix> ", cursor)
        session.write(b"reboot\r")
        cursor = session.wait_for(b"reboot\r\nRebooting...\r\n", cursor)
        cursor = session.wait_for(b"Nixodria OS\r\nType help.\r\nnix> ", cursor)
        cursor = assert_editor_document(session, full_document, cursor)
        halt(session, cursor)

    return full_document


def exercise_process_restart(qemu: str, image: Path, expected: bytes) -> bytes:
    replacement = b"saved across\r\nfull process restarts"
    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        cursor = assert_editor_document(session, expected, cursor)

        session.write(b"edit\r")
        cursor = session.wait_for(b"edit\r\n" + EDITOR_FRAME + expected, cursor)
        session.write(b"\x0c")
        cursor = session.wait_for(EDITOR_FRAME, cursor)
        session.write(replacement + b"\x13")
        cursor = session.wait_for(
            replacement + EDITOR_SAVED_FRAME + replacement, cursor
        )
        # End QEMU immediately after the guest acknowledges the save. The next
        # process must recover it without an editor exit, guest halt, or delay.
    return replacement


def exercise_readonly_failure(qemu: str, image: Path, expected: bytes) -> None:
    before = image.read_bytes()
    with QemuSession(qemu, image, readonly=True) as session:
        cursor = session.wait_for(b"nix> ", 0)
        session.write(b"edit\r")
        cursor = session.wait_for(b"edit\r\n" + EDITOR_FRAME + expected, cursor)
        changed = expected + b"!"
        session.write(b"!\x13")
        cursor = session.wait_for(b"!" + EDITOR_FAILED_FRAME + changed, cursor)
        session.write(b"\x18")
        cursor = session.wait_for(b"\r\nnix> ", cursor)
        halt(session, cursor)
    if image.read_bytes() != before:
        raise SmokeFailure("read-only save attempt changed the disk image")
    assert_document_on_boot(qemu, image, expected)


def assert_document_on_boot(qemu: str, image: Path, expected: bytes) -> None:
    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        cursor = assert_editor_document(session, expected, cursor)
        halt(session, cursor)


def exercise_injected_write_failures(
    qemu: str, source: Path, directory: Path, expected: bytes
) -> None:
    original = source.read_bytes()
    slot_b = slice(SLOT_HEADER_OFFSETS[1], SLOT_HEADER_OFFSETS[1] + SLOT_SIZE)
    configurations = {
        "invalidation": """
[inject-error]
event = "write_aio"
errno = "5"
sector = "4"
""",
        "second-payload-sector": """
[inject-error]
event = "write_aio"
errno = "5"
sector = "6"
""",
        "final-header": """
[set-state]
event = "write_aio"
state = "1"
new_state = "2"

[inject-error]
event = "write_aio"
state = "2"
errno = "5"
sector = "4"
""",
    }

    for name, configuration in configurations.items():
        image = directory / f"fault-{name}.img"
        config = directory / f"fault-{name}.conf"
        image.write_bytes(original)
        config.write_text(configuration.strip() + "\n", encoding="utf-8")
        candidate = f"candidate blocked at {name}".encode()

        with QemuSession(qemu, image, blkdebug_config=config) as session:
            cursor = session.wait_for(b"nix> ", 0)
            session.write(b"edit\r")
            cursor = session.wait_for(b"edit\r\n" + EDITOR_FRAME + expected, cursor)
            session.write(b"\x0c")
            cursor = session.wait_for(EDITOR_FRAME, cursor)
            session.write(candidate + b"\x13")
            cursor = session.wait_for(
                candidate + EDITOR_FAILED_FRAME + candidate, cursor
            )
            session.write(b"\x18")
            cursor = session.wait_for(b"\r\nnix> ", cursor)
            halt(session, cursor)

        failed_image = image.read_bytes()
        if failed_image[slot_b] != original[slot_b]:
            raise SmokeFailure(f"{name} failure changed the active slot")
        if name == "invalidation":
            if failed_image != original:
                raise SmokeFailure("failed invalidation changed the disk image")
        else:
            header = failed_image[
                SLOT_HEADER_OFFSETS[0] : SLOT_PAYLOAD_OFFSETS[0]
            ]
            if any(header):
                raise SmokeFailure(f"{name} failure left a valid-looking target header")
            payload = failed_image[
                SLOT_PAYLOAD_OFFSETS[0] : SLOT_PAYLOAD_OFFSETS[0] + 2048
            ]
            padded = candidate + bytes(2048 - len(candidate))
            if name == "second-payload-sector" and payload[:512] != padded[:512]:
                raise SmokeFailure("payload failure did not commit its first sector")
            if name == "final-header" and payload != padded:
                raise SmokeFailure("final-header failure did not reach its commit stage")
        assert_document_on_boot(qemu, image, expected)


def write_variant(
    qemu: str,
    directory: Path,
    name: str,
    data: bytearray,
    expected: bytes,
) -> None:
    image = directory / f"recovery-{name}.img"
    image.write_bytes(data)
    assert_document_on_boot(qemu, image, expected)


def save_after_recovery(
    qemu: str, image: Path, recovered: bytes, replacement: bytes
) -> None:
    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        session.write(b"edit\r")
        cursor = session.wait_for(b"edit\r\n" + EDITOR_FRAME + recovered, cursor)
        session.write(b"\x0c")
        cursor = session.wait_for(EDITOR_FRAME, cursor)
        session.write(replacement + b"\x13")
        cursor = session.wait_for(
            replacement + EDITOR_SAVED_FRAME + replacement, cursor
        )
        session.write(b"\x18")
        cursor = session.wait_for(b"\r\nnix> ", cursor)
        halt(session, cursor)
    assert_document_on_boot(qemu, image, replacement)


def exercise_recovery(
    qemu: str,
    source: Path,
    directory: Path,
    older: bytes,
    newest: bytes,
) -> None:
    original = source.read_bytes()
    variants: dict[str, bytearray] = {}
    newest_header = SLOT_HEADER_OFFSETS[1]
    newest_payload = SLOT_PAYLOAD_OFFSETS[1]

    bad_payload = bytearray(original)
    bad_payload[newest_payload] ^= 0x01
    variants["newest-payload"] = bad_payload

    bad_magic = bytearray(original)
    bad_magic[newest_header] ^= 0x01
    variants["newest-magic"] = bad_magic

    bad_payload_crc = bytearray(original)
    bad_payload_crc[newest_header + 8] ^= 0x01
    variants["newest-payload-crc"] = bad_payload_crc

    bad_header_crc = bytearray(original)
    bad_header_crc[newest_header + 10] ^= 0x01
    variants["newest-header-crc"] = bad_header_crc

    bad_length = bytearray(original)
    bad_length[newest_header + 6 : newest_header + 8] = (2048).to_bytes(
        2, "little"
    )
    bad_length[newest_header + 10 : newest_header + 12] = checksum16(
        bad_length[newest_header : newest_header + 10]
    ).to_bytes(2, "little")
    variants["newest-oversized-length"] = bad_length

    for name, data in variants.items():
        write_variant(qemu, directory, name, data, older)

    # Model every important interrupted-save state while a third save targets
    # slot A. Slot B must remain the recoverable active version throughout.
    candidate = build_record(2, b"candidate after interrupted save")
    candidate_header = candidate[:SECTOR_SIZE]
    candidate_payload = candidate[SECTOR_SIZE:]
    target_header = SLOT_HEADER_OFFSETS[0]
    target_payload = SLOT_PAYLOAD_OFFSETS[0]

    invalidated = bytearray(original)
    invalidated[target_header : target_header + SECTOR_SIZE] = bytes(SECTOR_SIZE)
    write_variant(qemu, directory, "target-header-invalidated", invalidated, newest)

    for sectors in (2, 4):
        partial = bytearray(invalidated)
        count = sectors * SECTOR_SIZE
        partial[target_payload : target_payload + count] = candidate_payload[:count]
        write_variant(
            qemu,
            directory,
            f"target-payload-{sectors}-sectors",
            partial,
            newest,
        )

    for prefix in (1, 7, 10, 11):
        torn = bytearray(invalidated)
        torn[target_payload : target_payload + 2048] = candidate_payload
        torn[target_header : target_header + prefix] = candidate_header[:prefix]
        write_variant(qemu, directory, f"target-header-prefix-{prefix}", torn, newest)

    completed = bytearray(original)
    install_record(completed, 0, candidate)
    write_variant(
        qemu,
        directory,
        "completed-new-generation",
        completed,
        b"candidate after interrupted save",
    )

    newest_a_corrupt = bytearray(completed)
    newest_a_corrupt[SLOT_PAYLOAD_OFFSETS[0]] ^= 0x01
    fallback_image = directory / "recovery-newest-a-payload.img"
    fallback_image.write_bytes(newest_a_corrupt)
    assert_document_on_boot(qemu, fallback_image, newest)
    recovered_replacement = b"saved after fallback recovery"
    save_after_recovery(qemu, fallback_image, newest, recovered_replacement)
    recovered = newest_record(fallback_image.read_bytes())
    if recovered != (0, 2, recovered_replacement):
        raise SmokeFailure(f"post-fallback save used wrong slot/generation: {recovered!r}")

    both_payloads_bad = bytearray(completed)
    both_payloads_bad[SLOT_PAYLOAD_OFFSETS[0]] ^= 0x01
    both_payloads_bad[SLOT_PAYLOAD_OFFSETS[1]] ^= 0x01
    write_variant(qemu, directory, "both-payloads-invalid", both_payloads_bad, b"")

    both_invalid = bytearray(original)
    for offset in SLOT_HEADER_OFFSETS:
        both_invalid[offset : offset + SECTOR_SIZE] = bytes(SECTOR_SIZE)
    write_variant(qemu, directory, "both-slots-invalid", both_invalid, b"")


def exercise_generation_order(qemu: str, template: bytes, directory: Path) -> None:
    cases = (
        ("wrap-to-b", 0xFFFF, b"A old", 0, b"B new", b"B new"),
        ("wrap-to-a", 0, b"A new", 0xFFFF, b"B old", b"A new"),
        ("equal-prefers-a", 7, b"A tie", 7, b"B tie", b"A tie"),
        ("half-range-prefers-a", 0x8000, b"A half", 0, b"B half", b"A half"),
    )
    for name, generation_a, document_a, generation_b, document_b, expected in cases:
        image_data = bytearray(template)
        install_record(image_data, 0, build_record(generation_a, document_a))
        install_record(image_data, 1, build_record(generation_b, document_b))
        write_variant(qemu, directory, name, image_data, expected)

    writer_wrap = bytearray(template)
    install_record(writer_wrap, 0, build_record(0xFFFF, b"before writer wrap"))
    writer_image = directory / "generation-writer-wrap.img"
    writer_image.write_bytes(writer_wrap)
    save_after_recovery(
        qemu, writer_image, b"before writer wrap", b"after writer wrap"
    )
    wrapped = newest_record(writer_image.read_bytes())
    if wrapped != (1, 0, b"after writer wrap"):
        raise SmokeFailure(f"writer generation did not wrap correctly: {wrapped!r}")


def save_empty_and_verify_restart(qemu: str, image: Path, expected: bytes) -> None:
    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        session.write(b"edit\r")
        cursor = session.wait_for(b"edit\r\n" + EDITOR_FRAME + expected, cursor)
        session.write(b"\x0c")
        cursor = session.wait_for(EDITOR_FRAME, cursor)
        session.write(b"\x13")
        cursor = session.wait_for(EDITOR_SAVED_FRAME, cursor)
        session.write(b"\x18")
        cursor = session.wait_for(b"\r\nnix> ", cursor)
        halt(session, cursor)

    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        cursor = assert_editor_document(session, b"", cursor)
        halt(session, cursor)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} IMAGE", file=sys.stderr)
        return 2

    source_image = Path(sys.argv[1]).resolve()
    qemu_name = os.environ.get("QEMU", "qemu-system-i386")
    qemu = shutil.which(qemu_name)
    if qemu is None:
        print(f"smoke: QEMU executable not found: {qemu_name}", file=sys.stderr)
        return 1

    try:
        source_before = source_image.read_bytes()
        system = source_before[:STORAGE_OFFSET]
        with tempfile.TemporaryDirectory(prefix="nixodria-smoke-") as directory:
            runtime_image = Path(directory) / "nixodria.img"
            shutil.copyfile(source_image, runtime_image)

            full_document = exercise_editor_and_save(qemu, runtime_image)
            first_save = assert_saved_record(
                runtime_image,
                full_document,
                system,
                expected_slot=0,
                expected_generation=0,
            )
            if any(first_save[SLOT_HEADER_OFFSETS[1] :]):
                raise SmokeFailure("first save unexpectedly changed slot B")

            replacement = exercise_process_restart(qemu, runtime_image, full_document)
            second_save = assert_saved_record(
                runtime_image,
                replacement,
                system,
                expected_slot=1,
                expected_generation=1,
            )
            first_slot = slice(SLOT_HEADER_OFFSETS[0], SLOT_HEADER_OFFSETS[0] + SLOT_SIZE)
            if second_save[first_slot] != first_save[first_slot]:
                raise SmokeFailure("second save changed the active fallback slot")

            readonly_image = Path(directory) / "readonly.img"
            shutil.copyfile(runtime_image, readonly_image)
            exercise_readonly_failure(qemu, readonly_image, replacement)
            exercise_injected_write_failures(
                qemu, runtime_image, Path(directory), replacement
            )

            exercise_recovery(
                qemu,
                runtime_image,
                Path(directory),
                full_document,
                replacement,
            )
            exercise_generation_order(qemu, source_before, Path(directory))

            save_empty_and_verify_restart(qemu, runtime_image, replacement)
            third_save = assert_saved_record(
                runtime_image,
                b"",
                system,
                expected_slot=0,
                expected_generation=2,
            )
            second_slot = slice(SLOT_HEADER_OFFSETS[1], SLOT_HEADER_OFFSETS[1] + SLOT_SIZE)
            if third_save[second_slot] != second_save[second_slot]:
                raise SmokeFailure("third save changed the active fallback slot")

        if source_image.read_bytes() != source_before:
            raise SmokeFailure("smoke test modified the source build image")
    except (BrokenPipeError, OSError, SmokeFailure) as error:
        print(f"smoke: {error}", file=sys.stderr)
        return 1

    print(
        "smoke: editor persistence, restart, corruption recovery, and write faults passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
