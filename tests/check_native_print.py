#!/usr/bin/env python3
"""Exercise Nixodria's native NE2000/IPP/PWG Raster print path."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import selectors
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

from smoke import (
    IMAGE_SIZE,
    SNAPSHOT_SIZE,
    STORAGE_END,
    STORAGE_OFFSET,
    QemuSession,
    build_snapshot,
    halt,
    install_snapshot,
)


GUEST_PRINTER_IP = "10.0.2.100"
IPP_PORT = 631
PWG_HEADER_SIZE = 1796
PWG_WIDTH = 2550
PWG_HEIGHT = 3300
PWG_RESOLUTION = 300
MAX_HTTP_HEADER = 64 * 1024
MAX_HTTP_BODY = 32 * 1024 * 1024
IPP_SUCCESSFUL_OK = 0x0000
IPP_CLIENT_ERROR_DOCUMENT_FORMAT_NOT_SUPPORTED = 0x040A


class NativePrintFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class HTTPRequest:
    method: str
    target: str
    version: str
    headers: dict[str, str]
    body: bytes


def _recv_more(connection: socket.socket, buffer: bytearray) -> None:
    chunk = connection.recv(65536)
    if not chunk:
        raise NativePrintFailure("printer connection closed unexpectedly")
    buffer.extend(chunk)


def _take_line(connection: socket.socket, buffer: bytearray) -> bytes:
    while True:
        end = buffer.find(b"\r\n")
        if end >= 0:
            line = bytes(buffer[:end])
            del buffer[: end + 2]
            return line
        if len(buffer) > MAX_HTTP_HEADER:
            raise NativePrintFailure("HTTP line is unreasonably large")
        _recv_more(connection, buffer)


def _read_chunked_body(
    connection: socket.socket, initial: bytes
) -> bytes:
    buffer = bytearray(initial)
    body = bytearray()
    while True:
        size_line = _take_line(connection, buffer)
        size_text = size_line.split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError as error:
            raise NativePrintFailure(
                f"invalid HTTP chunk size {size_line!r}"
            ) from error
        if size < 0 or len(body) + size > MAX_HTTP_BODY:
            raise NativePrintFailure("chunked IPP request is too large")
        if size == 0:
            while _take_line(connection, buffer):
                pass
            if buffer:
                raise NativePrintFailure(
                    "unexpected bytes after the chunked request trailers"
                )
            return bytes(body)
        while len(buffer) < size + 2:
            _recv_more(connection, buffer)
        body.extend(buffer[:size])
        if buffer[size : size + 2] != b"\r\n":
            raise NativePrintFailure("HTTP chunk is missing its final CRLF")
        del buffer[: size + 2]


def _read_http_request(connection: socket.socket) -> HTTPRequest:
    buffer = bytearray()
    while True:
        end = buffer.find(b"\r\n\r\n")
        if end >= 0:
            header_block = bytes(buffer[:end])
            remainder = bytes(buffer[end + 4 :])
            break
        if len(buffer) > MAX_HTTP_HEADER:
            raise NativePrintFailure("HTTP request headers are too large")
        _recv_more(connection, buffer)

    lines = header_block.split(b"\r\n")
    try:
        method, target, version = lines[0].decode("ascii").split(" ")
    except (UnicodeDecodeError, ValueError) as error:
        raise NativePrintFailure("malformed HTTP request line") from error

    headers: dict[str, str] = {}
    for raw_line in lines[1:]:
        name, separator, value = raw_line.partition(b":")
        if not separator:
            raise NativePrintFailure(f"malformed HTTP header {raw_line!r}")
        try:
            normalized_name = name.decode("ascii").strip().lower()
            normalized_value = value.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise NativePrintFailure("non-ASCII HTTP header") from error
        if normalized_name in headers:
            raise NativePrintFailure(
                f"duplicate HTTP header {normalized_name!r}"
            )
        headers[normalized_name] = normalized_value

    if headers.get("expect", "").lower() == "100-continue":
        connection.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
    transfer_encodings = [
        value.strip().lower()
        for value in headers.get("transfer-encoding", "").split(",")
        if value.strip()
    ]
    if transfer_encodings != ["chunked"]:
        raise NativePrintFailure(
            "native print request must use Transfer-Encoding: chunked"
        )
    if "content-length" in headers:
        raise NativePrintFailure(
            "chunked native print request must not send Content-Length"
        )
    body = _read_chunked_body(connection, remainder)
    return HTTPRequest(method, target, version, headers, body)


class FakeIPPServer:
    def __init__(self, ipp_status: int = IPP_SUCCESSFUL_OK) -> None:
        if not 0 <= ipp_status <= 0xFFFF:
            raise ValueError(f"invalid IPP status 0x{ipp_status:x}")
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(60.0)
        self.port = self.listener.getsockname()[1]
        self.ipp_status = ipp_status
        self.request: HTTPRequest | None = None
        self.queued_jobs: list[HTTPRequest] = []
        self.error: BaseException | None = None
        self.thread = threading.Thread(
            target=self._serve,
            name="fake-ipp-printer",
            daemon=True,
        )

    def __enter__(self) -> "FakeIPPServer":
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _serve(self) -> None:
        try:
            connection, _ = self.listener.accept()
            with connection:
                connection.settimeout(60.0)
                self.request = _read_http_request(connection)
                request_id = self.request.body[4:8]
                if len(request_id) != 4:
                    raise NativePrintFailure("IPP request lacks a request-id")
                if self.ipp_status < 0x0100:
                    self.queued_jobs.append(self.request)
                ipp_response = (
                    b"\x01\x01"
                    + self.ipp_status.to_bytes(2, "big")
                    + request_id
                    + b"\x03"
                )
                response = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/ipp\r\n"
                    b"Content-Length: 9\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                    + ipp_response
                )
                connection.sendall(response)
        except BaseException as error:  # surfaced on the test thread
            self.error = error
        finally:
            self.listener.close()

    def close(self) -> None:
        if self.listener.fileno() >= 0:
            self.listener.close()
        self.thread.join(timeout=5.0)

    def result(self) -> HTTPRequest:
        self.thread.join(timeout=5.0)
        if self.thread.is_alive():
            raise NativePrintFailure("fake IPP printer did not finish")
        if self.error is not None:
            raise NativePrintFailure(
                f"fake IPP printer failed: {self.error}"
            ) from self.error
        if self.request is None:
            raise NativePrintFailure("fake IPP printer received no request")
        return self.request


class NetworkQemuSession:
    def __init__(
        self, qemu: str, image: Path, host_port: int | None
    ) -> None:
        if host_port is None:
            guest_network = "user,id=nixnet,ipv6=off"
        else:
            guest_network = (
                f"user,id=nixnet,"
                f"guestfwd=tcp:{GUEST_PRINTER_IP}:{IPP_PORT}-"
                f"tcp:127.0.0.1:{host_port}"
            )
        command = [
            qemu,
            "-accel",
            "tcg",
            "-boot",
            "a",
            "-drive",
            f"format=raw,file={image},if=floppy,cache=writethrough",
            "-display",
            "none",
            "-serial",
            "stdio",
            "-monitor",
            "none",
            "-netdev",
            guest_network,
            "-device",
            "ne2k_isa,netdev=nixnet,iobase=0x300,irq=9,mac=52:54:00:12:34:56",
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
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def __enter__(self) -> "NetworkQemuSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def write(self, data: bytes) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def wait_for(
        self, expected: bytes, start: int, timeout: float = 10.0
    ) -> int:
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
        rendered = bytes(self.transcript[-4096:]).decode(
            "utf-8", errors="replace"
        )
        raise NativePrintFailure(
            f"timed out waiting for {expected!r}{suffix}\n"
            f"--- session transcript tail ---\n{rendered}"
        )

    def close(self) -> None:
        self.selector.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2.0)


def _parse_ipp_request(body: bytes) -> bytes:
    if len(body) < 9:
        raise NativePrintFailure("IPP request is truncated")
    if body[:2] != b"\x01\x01":
        raise NativePrintFailure(
            f"IPP version is {body[:2]!r}, expected 1.1"
        )
    operation = int.from_bytes(body[2:4], "big")
    if operation != 0x0002:
        raise NativePrintFailure(
            f"IPP operation is 0x{operation:04x}, expected Print-Job"
        )
    request_id = int.from_bytes(body[4:8], "big")
    if request_id != 1:
        raise NativePrintFailure(
            f"IPP request-id is {request_id}, expected 1"
        )

    position = 8
    group_tag: int | None = None
    previous_name: bytes | None = None
    attributes: dict[tuple[int, bytes], list[bytes]] = {}
    while position < len(body):
        tag = body[position]
        position += 1
        if tag == 0x03:
            break
        if tag <= 0x0F:
            group_tag = tag
            previous_name = None
            continue
        if group_tag is None or position + 2 > len(body):
            raise NativePrintFailure("malformed IPP attribute group")
        name_length = int.from_bytes(body[position : position + 2], "big")
        position += 2
        if position + name_length + 2 > len(body):
            raise NativePrintFailure("truncated IPP attribute name")
        if name_length:
            name = body[position : position + name_length]
            previous_name = name
        elif previous_name is not None:
            name = previous_name
        else:
            raise NativePrintFailure("IPP continuation has no attribute name")
        position += name_length
        value_length = int.from_bytes(body[position : position + 2], "big")
        position += 2
        if position + value_length > len(body):
            raise NativePrintFailure("truncated IPP attribute value")
        value = body[position : position + value_length]
        position += value_length
        attributes.setdefault((group_tag, name), []).append(value)
    else:
        raise NativePrintFailure("IPP request has no end-of-attributes tag")

    document_formats = attributes.get((0x01, b"document-format"), [])
    if document_formats != [b"image/pwg-raster"]:
        raise NativePrintFailure(
            "IPP document-format is not exactly image/pwg-raster"
        )
    printer_uris = attributes.get((0x01, b"printer-uri"), [])
    expected_uri = f"ipp://{GUEST_PRINTER_IP}:{IPP_PORT}/ipp/print".encode()
    if printer_uris != [expected_uri]:
        raise NativePrintFailure(
            f"IPP printer-uri is {printer_uris!r}, expected {expected_uri!r}"
        )
    document = body[position:]
    if not document:
        raise NativePrintFailure("IPP Print-Job has no document data")
    return document


def _be32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def _validate_pwg_header(header: bytes, page_number: int) -> int:
    if len(header) != PWG_HEADER_SIZE:
        raise NativePrintFailure(f"PWG page {page_number} header is truncated")
    if header[:9] != b"PwgRaster" or any(header[9:64]):
        raise NativePrintFailure(
            f"PWG page {page_number} lacks the PwgRaster media-class marker"
        )

    required = {
        276: PWG_RESOLUTION,
        280: PWG_RESOLUTION,
        372: PWG_WIDTH,
        376: PWG_HEIGHT,
        384: 8,
        388: 8,
        392: PWG_WIDTH,
        396: 0,
        400: 18,
        420: 1,
        456: 1,
        460: 1,
    }
    for offset, expected in required.items():
        actual = _be32(header, offset)
        if actual != expected:
            raise NativePrintFailure(
                f"PWG page {page_number} field at offset {offset} is "
                f"{actual}, expected {expected}"
            )
    if _be32(header, 272) != 0 or _be32(header, 368) != 0:
        raise NativePrintFailure(f"PWG page {page_number} is not one-sided")
    total_pages = _be32(header, 452)
    if total_pages < 1:
        raise NativePrintFailure(
            f"PWG page {page_number} has an invalid page count"
        )
    if any(header[464:516]):
        raise NativePrintFailure(
            f"PWG page {page_number} has nonzero reserved integer fields"
        )
    if any(header[516:1604]):
        raise NativePrintFailure(
            f"PWG page {page_number} has nonzero reserved extension fields"
        )
    return total_pages


def _decode_pwg_page(
    document: bytes, position: int, page_number: int
) -> tuple[int, int, int]:
    header_end = position + PWG_HEADER_SIZE
    if header_end > len(document):
        raise NativePrintFailure(f"PWG page {page_number} header is truncated")
    header = document[position:header_end]
    total_pages = _validate_pwg_header(header, page_number)
    position = header_end
    rows = 0
    nonwhite_pixels = 0
    while rows < PWG_HEIGHT:
        if position >= len(document):
            raise NativePrintFailure(
                f"PWG page {page_number} ended after {rows} rows"
            )
        repeated_rows = document[position] + 1
        position += 1
        decoded_columns = 0
        row_nonwhite = 0
        while decoded_columns < PWG_WIDTH:
            if position >= len(document):
                raise NativePrintFailure(
                    f"PWG page {page_number} has a truncated PackBits row"
                )
            control = document[position]
            position += 1
            if control <= 127:
                count = control + 1
                if position >= len(document):
                    raise NativePrintFailure(
                        f"PWG page {page_number} has a truncated repeated run"
                    )
                pixel = document[position]
                position += 1
                if pixel != 255:
                    row_nonwhite += count
            elif control >= 129:
                count = 257 - control
                end = position + count
                if end > len(document):
                    raise NativePrintFailure(
                        f"PWG page {page_number} has a truncated literal run"
                    )
                row_nonwhite += sum(pixel != 255 for pixel in document[position:end])
                position = end
            else:
                raise NativePrintFailure(
                    f"PWG page {page_number} used reserved PackBits control 128"
                )
            decoded_columns += count
            if decoded_columns > PWG_WIDTH:
                raise NativePrintFailure(
                    f"PWG page {page_number} row expands past {PWG_WIDTH} pixels"
                )
        rows += repeated_rows
        if rows > PWG_HEIGHT:
            raise NativePrintFailure(
                f"PWG page {page_number} repeats past {PWG_HEIGHT} rows"
            )
        nonwhite_pixels += row_nonwhite * repeated_rows
    return position, total_pages, nonwhite_pixels


def validate_pwg_raster(
    document: bytes, *, require_nonwhite: bool = True
) -> tuple[int, int]:
    if not document.startswith(b"RaS2"):
        raise NativePrintFailure("Print-Job document lacks the PWG RaS2 sync word")
    position = 4
    page_number = 0
    declared_pages: int | None = None
    total_nonwhite = 0
    while position < len(document):
        page_number += 1
        position, page_count, nonwhite = _decode_pwg_page(
            document, position, page_number
        )
        if declared_pages is None:
            declared_pages = page_count
        elif page_count != declared_pages:
            raise NativePrintFailure("PWG pages disagree about the total page count")
        total_nonwhite += nonwhite
    if declared_pages is None or page_number != declared_pages:
        raise NativePrintFailure(
            f"PWG stream contains {page_number} pages but declares {declared_pages}"
        )
    if require_nonwhite and total_nonwhite == 0:
        raise NativePrintFailure("PWG raster contains no printed pixels")
    return page_number, total_nonwhite


def exercise_native_print(qemu: str, template_path: Path) -> tuple[int, int]:
    template = template_path.read_bytes()
    if len(template) != IMAGE_SIZE:
        raise NativePrintFailure(
            f"image has {len(template)} bytes, expected {IMAGE_SIZE}"
        )
    # The first 186-byte line wraps into two exact 93-column rows. Together
    # with 95 more lines this crosses the 96-line page boundary by exactly one
    # row and generates enough acknowledgements to
    # wrap the NE2000 receive ring several times.
    logical_lines = [b"W" * 186]
    logical_lines.extend(
        f"LINE {line:03d} PRINT".encode("ascii") for line in range(95)
    )
    document = b"\r\n".join(logical_lines)
    image_data = bytearray(template)
    snapshot = build_snapshot(7, ((b"NOTES.TXT", document),))
    if len(snapshot) != SNAPSHOT_SIZE:
        raise NativePrintFailure("test snapshot builder returned the wrong size")
    install_snapshot(image_data, 0, snapshot)

    with tempfile.TemporaryDirectory(prefix="nixodria-native-print-") as directory:
        runtime_image = Path(directory) / "nixodria.img"
        runtime_image.write_bytes(image_data)
        before = runtime_image.read_bytes()

        with FakeIPPServer() as printer:
            session_error: BaseException | None = None
            try:
                with NetworkQemuSession(qemu, runtime_image, printer.port) as session:
                    cursor = session.wait_for(b"nix> ", 0, timeout=15.0)
                    session.write(b"printer 10.0.2.100\r")
                    cursor = session.wait_for(
                        b"printer 10.0.2.100\r\n"
                        b"Printer configured.\r\n"
                        b"nix> ",
                        cursor,
                    )
                    session.write(b"print NOTES.TXT\r")
                    cursor = session.wait_for(
                        b"print NOTES.TXT\r\n"
                        b"Print job queued.\r\n"
                        b"nix> ",
                        cursor,
                        timeout=90.0,
                    )
                    session.write(b"halt\r")
                    session.wait_for(
                        b"halt\r\nHalted.\r\n", cursor, timeout=10.0
                    )
            except BaseException as error:
                session_error = error
            try:
                request = printer.result()
            except BaseException as printer_error:
                if session_error is not None:
                    raise NativePrintFailure(
                        f"{session_error}\nprinter detail: {printer_error}"
                    ) from session_error
                raise
            if session_error is not None:
                raise session_error

        after = runtime_image.read_bytes()
        if after[STORAGE_OFFSET:STORAGE_END] != before[STORAGE_OFFSET:STORAGE_END]:
            raise NativePrintFailure("printing mutated the saved-file snapshots")
        if after != before:
            raise NativePrintFailure("printing mutated the floppy image")

    if (request.method, request.target, request.version) != (
        "POST",
        "/ipp/print",
        "HTTP/1.1",
    ):
        raise NativePrintFailure(
            "native print request is not HTTP/1.1 POST /ipp/print"
        )
    content_type = request.headers.get("content-type", "")
    if content_type.lower() != "application/ipp":
        raise NativePrintFailure(
            f"HTTP Content-Type is {content_type!r}, expected application/ipp"
        )
    raster = _parse_ipp_request(request.body)
    pages, nonwhite = validate_pwg_raster(raster)
    if pages != 2:
        raise NativePrintFailure(
            f"wrapped test document produced {pages} pages, expected 2"
        )
    return pages, nonwhite


def exercise_blank_native_print(qemu: str, template_path: Path) -> None:
    image_data = bytearray(template_path.read_bytes())
    install_snapshot(image_data, 0, build_snapshot(8, ((b"EMPTY.TXT", b""),)))
    with tempfile.TemporaryDirectory(prefix="nixodria-blank-print-") as directory:
        runtime_image = Path(directory) / "nixodria.img"
        runtime_image.write_bytes(image_data)
        before = runtime_image.read_bytes()
        with FakeIPPServer() as printer:
            with NetworkQemuSession(qemu, runtime_image, printer.port) as session:
                cursor = session.wait_for(b"nix> ", 0, timeout=15.0)
                session.write(b"printer 10.0.2.100\r")
                cursor = session.wait_for(
                    b"printer 10.0.2.100\r\n"
                    b"Printer configured.\r\n"
                    b"nix> ",
                    cursor,
                )
                session.write(b"print EMPTY.TXT\r")
                cursor = session.wait_for(
                    b"print EMPTY.TXT\r\n"
                    b"Print job queued.\r\n"
                    b"nix> ",
                    cursor,
                    timeout=60.0,
                )
                session.write(b"halt\r")
                session.wait_for(
                    b"halt\r\nHalted.\r\n", cursor, timeout=10.0
                )
            request = printer.result()
        if runtime_image.read_bytes() != before:
            raise NativePrintFailure("blank printing mutated the floppy image")
    raster = _parse_ipp_request(request.body)
    pages, nonwhite = validate_pwg_raster(raster, require_nonwhite=False)
    if pages != 1 or nonwhite != 0:
        raise NativePrintFailure(
            f"empty file produced {pages} page(s) and {nonwhite} dark pixels"
        )


def exercise_native_print_rejection(qemu: str, template_path: Path) -> int:
    template = template_path.read_bytes()
    if len(template) != IMAGE_SIZE:
        raise NativePrintFailure(
            f"image has {len(template)} bytes, expected {IMAGE_SIZE}"
        )
    image_data = bytearray(template)
    snapshot = build_snapshot(8, ((b"REJECT.TXT", b"ONE LINE ONLY"),))
    if len(snapshot) != SNAPSHOT_SIZE:
        raise NativePrintFailure("test snapshot builder returned the wrong size")
    install_snapshot(image_data, 0, snapshot)

    with tempfile.TemporaryDirectory(prefix="nixodria-native-reject-") as directory:
        runtime_image = Path(directory) / "nixodria.img"
        runtime_image.write_bytes(image_data)
        before = runtime_image.read_bytes()

        with FakeIPPServer(
            IPP_CLIENT_ERROR_DOCUMENT_FORMAT_NOT_SUPPORTED
        ) as printer:
            session_error: BaseException | None = None
            command_transcript = b""
            try:
                with NetworkQemuSession(qemu, runtime_image, printer.port) as session:
                    cursor = session.wait_for(b"nix> ", 0, timeout=15.0)
                    session.write(b"printer 10.0.2.100\r")
                    cursor = session.wait_for(
                        b"printer 10.0.2.100\r\n"
                        b"Printer configured.\r\n"
                        b"nix> ",
                        cursor,
                    )
                    transcript_start = len(session.transcript)
                    session.write(b"print REJECT.TXT\r")
                    cursor = session.wait_for(
                        b"print REJECT.TXT\r\n"
                        b"Printer rejected job.\r\n"
                        b"nix> ",
                        cursor,
                        timeout=60.0,
                    )
                    command_transcript = bytes(
                        session.transcript[transcript_start:cursor]
                    )
                    session.write(b"halt\r")
                    session.wait_for(
                        b"halt\r\nHalted.\r\n", cursor, timeout=10.0
                    )
            except BaseException as error:
                session_error = error
            try:
                request = printer.result()
            except BaseException as printer_error:
                if session_error is not None:
                    raise NativePrintFailure(
                        f"{session_error}\nprinter detail: {printer_error}"
                    ) from session_error
                raise
            if session_error is not None:
                raise session_error
            if printer.queued_jobs:
                raise NativePrintFailure(
                    "fake printer queued a client-error Print-Job"
                )

        if b"Print job queued." in command_transcript:
            raise NativePrintFailure(
                "guest reported a rejected Print-Job as queued"
            )
        after = runtime_image.read_bytes()
        if after[STORAGE_OFFSET:STORAGE_END] != before[STORAGE_OFFSET:STORAGE_END]:
            raise NativePrintFailure(
                "rejected printing mutated the saved-file snapshots"
            )
        if after != before:
            raise NativePrintFailure("rejected printing mutated the floppy image")

    raster = _parse_ipp_request(request.body)
    pages, _ = validate_pwg_raster(raster)
    if pages != 1:
        raise NativePrintFailure(
            f"one-line rejected job produced {pages} pages, expected 1"
        )
    return pages


def exercise_corrupt_module(qemu: str, template_path: Path) -> None:
    template = template_path.read_bytes()
    image_data = bytearray(template)
    install_snapshot(
        image_data,
        0,
        build_snapshot(9, ((b"CORRUPT.TXT", b"MUST NOT EXECUTE"),)),
    )
    # Damage module code while leaving its fixed header and stored checksum in
    # place. The resident kernel must reject it before the first instruction.
    image_data[STORAGE_END + 128] ^= 0x5A

    with tempfile.TemporaryDirectory(prefix="nixodria-corrupt-print-") as directory:
        runtime_image = Path(directory) / "nixodria.img"
        runtime_image.write_bytes(image_data)
        before = runtime_image.read_bytes()
        with QemuSession(qemu, runtime_image) as session:
            cursor = session.wait_for(b"nix> ", 0, timeout=15.0)
            transcript_start = len(session.transcript)
            session.write(b"print CORRUPT.TXT\r")
            cursor = session.wait_for(
                b"print CORRUPT.TXT\r\n"
                b"Printer module unavailable.\r\n"
                b"nix> ",
                cursor,
                timeout=15.0,
            )
            command_transcript = bytes(
                session.transcript[transcript_start:cursor]
            )
            if b"Print job queued." in command_transcript:
                raise NativePrintFailure(
                    "guest executed or accepted a corrupt printer module"
                )
            halt(session, cursor)
        if runtime_image.read_bytes() != before:
            raise NativePrintFailure(
                "refusing a corrupt printer module mutated the floppy image"
            )


def exercise_unavailable_printer(qemu: str, template_path: Path) -> None:
    image_data = bytearray(template_path.read_bytes())
    install_snapshot(
        image_data,
        0,
        build_snapshot(10, ((b"OFFLINE.TXT", b"NO NETWORK DEVICE"),)),
    )
    with tempfile.TemporaryDirectory(prefix="nixodria-offline-print-") as directory:
        runtime_image = Path(directory) / "nixodria.img"
        runtime_image.write_bytes(image_data)
        before = runtime_image.read_bytes()
        # The ordinary smoke session deliberately has no NIC. This proves a
        # valid module's AL=1 result is surfaced distinctly and fail-closed.
        with QemuSession(qemu, runtime_image) as session:
            cursor = session.wait_for(b"nix> ", 0, timeout=15.0)
            transcript_start = len(session.transcript)
            session.write(b"print OFFLINE.TXT\r")
            cursor = session.wait_for(
                b"print OFFLINE.TXT\r\n"
                b"Printer unavailable.\r\n"
                b"nix> ",
                cursor,
                timeout=15.0,
            )
            command_transcript = bytes(
                session.transcript[transcript_start:cursor]
            )
            if b"Print job queued." in command_transcript:
                raise NativePrintFailure(
                    "guest reported an unavailable printer as queued"
                )
            halt(session, cursor)
        if runtime_image.read_bytes() != before:
            raise NativePrintFailure(
                "an unavailable print attempt mutated the floppy image"
            )


def main() -> int:
    if len(sys.argv) != 2:
        print(
            f"usage: {Path(sys.argv[0]).name} IMAGE",
            file=sys.stderr,
        )
        return 2
    requested_qemu = os.environ.get("QEMU", "qemu-system-i386")
    qemu = shutil.which(requested_qemu)
    if qemu is None:
        print(f"native print: {requested_qemu} not found", file=sys.stderr)
        return 1
    try:
        pages, nonwhite = exercise_native_print(qemu, Path(sys.argv[1]))
        exercise_blank_native_print(qemu, Path(sys.argv[1]))
        rejected_pages = exercise_native_print_rejection(
            qemu, Path(sys.argv[1])
        )
        exercise_corrupt_module(qemu, Path(sys.argv[1]))
        exercise_unavailable_printer(qemu, Path(sys.argv[1]))
    except (NativePrintFailure, OSError, subprocess.SubprocessError) as error:
        print(f"native print: {error}", file=sys.stderr)
        return 1
    print(
        f"native print: queued {pages} PWG Raster page(s) with "
        f"{nonwhite} non-white pixels plus one blank page; "
        f"rejected {rejected_pages}-page "
        f"client-error job; offline printer and corrupt module refused; "
        f"floppy unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
