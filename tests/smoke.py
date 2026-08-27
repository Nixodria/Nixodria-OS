#!/usr/bin/env python3
"""Boot Nixodria OS in QEMU and exercise named files and durable snapshots."""

import os
from dataclasses import dataclass
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
IMAGE_SECTORS = 2880
SYSTEM_SECTORS = 11
SNAPSHOT_SECTORS = 33
SNAPSHOT_SIZE = SNAPSHOT_SECTORS * SECTOR_SIZE
STORAGE_OFFSET = SYSTEM_SECTORS * SECTOR_SIZE
SNAPSHOT_OFFSETS = (STORAGE_OFFSET, STORAGE_OFFSET + SNAPSHOT_SIZE)
SNAPSHOT_LBAS = (SYSTEM_SECTORS, SYSTEM_SECTORS + SNAPSHOT_SECTORS)
STORAGE_END = STORAGE_OFFSET + 2 * SNAPSHOT_SIZE
PRINT_MODULE_SECTORS = 32
BASIC_MODULE_OFFSET = STORAGE_END + PRINT_MODULE_SECTORS * SECTOR_SIZE
BASIC_MODULE_SECTORS = 16
PACKAGE_CATALOG_OFFSET = BASIC_MODULE_OFFSET + BASIC_MODULE_SECTORS * SECTOR_SIZE
PACKAGE_SLOT_SECTORS = 5
PACKAGE_SLOT_SIZE = PACKAGE_SLOT_SECTORS * SECTOR_SIZE
PACKAGE_SLOTS = 8
PACKAGE_CATALOG_END = PACKAGE_CATALOG_OFFSET + PACKAGE_SLOTS * PACKAGE_SLOT_SIZE
PACKAGE_SIGNATURE = b"NIXPKG1\0"
PACKAGE_LENGTH_OFFSET = 8
PACKAGE_SOURCE_CHECKSUM_OFFSET = 10
PACKAGE_HEADER_CHECKSUM_OFFSET = 12
PACKAGE_FILENAME_OFFSET = 16
PACKAGE_FILENAME_SIZE = 13
IMAGE_SIZE = IMAGE_SECTORS * SECTOR_SIZE
STORAGE_MAGIC = b"NIX3"
LEGACY_STORAGE_MAGIC = b"NIX2"
MAX_FILES = 8
FILENAME_SIZE = 13
FILENAME_MAX = FILENAME_SIZE - 1
ENTRY_OFFSET = 8
ENTRY_SIZE = 18
ENTRY_LENGTH_OFFSET = 14
ENTRY_CHECKSUM_OFFSET = 16
HEADER_CHECKSUM_OFFSET = ENTRY_OFFSET + MAX_FILES * ENTRY_SIZE
FILE_CAPACITY = 2048
DOCUMENT_MAX = FILE_CAPACITY - 1

CLEAR_SCREEN = b"\x1b[2J\x1b[H"
EDITOR_CONTROLS = (
    b"\r\nCtrl-S save | Ctrl-R run | Ctrl-X exit | Ctrl-L clear\r\n"
)
BASIC_FRAME = CLEAR_SCREEN + b"Nixodria BASIC\r\n\r\n"
BASIC_FINISHED = b"\r\nProgram finished. Press any key."
TETRIS_TITLE = b"TETRIS"
TETRIS_GAME_OVER = b"GAME OVER"
TETRIS_CONTROLS = b"a d w s space q"
TETRIS_FULL_ROW = (1 << 10) - 1


@dataclass(frozen=True)
class TetrisScreen:
    cursor: int
    rows: tuple[int, ...]
    score: int
    lines: int
    game_over: bool = False


@dataclass(frozen=True)
class Package:
    slot: int
    filename: bytes
    source: bytes


def normalize_filename(filename: bytes) -> bytes:
    normalized = filename.upper()
    if not 1 <= len(normalized) <= FILENAME_MAX:
        raise ValueError("filename is outside the on-disk format")
    if any(
        not (
            ord("A") <= value <= ord("Z")
            or ord("0") <= value <= ord("9")
            or value in b"._-"
        )
        for value in normalized
    ):
        raise ValueError("filename contains an unsupported byte")
    return normalized


def editor_header(filename: bytes) -> bytes:
    return (
        CLEAR_SCREEN
        + b"Nixodria Editor: "
        + normalize_filename(filename)
        + EDITOR_CONTROLS
    )


def editor_frame(filename: bytes, status: bytes = b"") -> bytes:
    frame = editor_header(filename)
    if status:
        frame += status + b"\r\n"
    return frame + b"\r\n"


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
            "-nic",
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
        rendered = bytes(self.transcript[-4096:]).decode(
            "utf-8", errors="replace"
        )
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
                raise SmokeFailure(
                    "QEMU closed its output instead of remaining halted"
                )
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


def parse_tetris_stat(line: bytes, prefix: bytes) -> int:
    if not line.startswith(prefix):
        raise SmokeFailure(
            f"Tetris status line {line!r} does not start with {prefix!r}"
        )
    value = line[len(prefix) :]
    if not value or any(byte < ord("0") or byte > ord("9") for byte in value):
        raise SmokeFailure(f"Tetris status line is not decimal: {line!r}")
    return int(value)


def wait_for_tetris_screen(
    session: QemuSession, start: int, timeout: float = 5.0
) -> TetrisScreen:
    clear_end = session.wait_for(CLEAR_SCREEN, start, timeout=timeout)
    screen_start = clear_end - len(CLEAR_SCREEN)
    title_end = session.wait_for(b"\r\n", clear_end, timeout=timeout)
    title = bytes(session.transcript[clear_end : title_end - 2])

    if title == TETRIS_TITLE:
        screen_end = session.wait_for(
            TETRIS_CONTROLS + b"\r\n", title_end, timeout=timeout
        )
        screen = bytes(session.transcript[screen_start:screen_end])
        physical_lines = screen[len(CLEAR_SCREEN) :].split(b"\r\n")
        if len(physical_lines) != 25 or physical_lines[-1] != b"":
            raise SmokeFailure(
                f"Tetris frame has {len(physical_lines) - 1} physical lines; "
                "expected 24"
            )
        if physical_lines[0] != TETRIS_TITLE:
            raise SmokeFailure("Tetris frame title changed while parsing")
        rows: list[int] = []
        for row_number, rendered in enumerate(physical_lines[1:21]):
            if (
                len(rendered) != 12
                or rendered[:1] != b"|"
                or rendered[-1:] != b"|"
                or any(value not in b"01" for value in rendered[1:-1])
            ):
                raise SmokeFailure(
                    f"Tetris row {row_number} is not a 10-cell binary row: "
                    f"{rendered!r}"
                )
            # The BASIC source prints each row least-significant bit first.
            rows.append(int(rendered[1:-1][::-1], 2))
        score = parse_tetris_stat(physical_lines[21], b"S ")
        lines = parse_tetris_stat(physical_lines[22], b"L ")
        if physical_lines[23] != TETRIS_CONTROLS:
            raise SmokeFailure("Tetris controls line is missing")
        return TetrisScreen(screen_end, tuple(rows), score, lines)

    if title == TETRIS_GAME_OVER:
        screen_end = session.wait_for(b"r/q\r\n", title_end, timeout=timeout)
        screen = bytes(session.transcript[screen_start:screen_end])
        physical_lines = screen[len(CLEAR_SCREEN) :].split(b"\r\n")
        if (
            len(physical_lines) != 5
            or physical_lines[0] != TETRIS_GAME_OVER
            or physical_lines[3] != b"r/q"
            or physical_lines[4] != b""
        ):
            raise SmokeFailure(f"malformed Tetris game-over frame: {screen!r}")
        score = parse_tetris_stat(physical_lines[1], b"S ")
        lines = parse_tetris_stat(physical_lines[2], b"L ")
        return TetrisScreen(screen_end, (), score, lines, game_over=True)

    raise SmokeFailure(f"unexpected full-screen BASIC output title: {title!r}")


def tetris_occupied_cells(screen: TetrisScreen) -> int:
    return sum(row.bit_count() for row in screen.rows)


def rotate_tetris_piece(piece: int) -> int:
    if piece in (15, 4369):
        return 4384 - piece
    if piece == 51:
        return piece
    rotated = 0
    for bit in range(12):
        if piece & (1 << bit):
            column = bit % 4
            row = bit // 4
            rotated |= 1 << (column * 4 + 2 - row)
    return rotated


def tetris_piece_valid(
    rows: tuple[int, ...], piece: int, x: int, y: int
) -> bool:
    if x < 0:
        return False
    for piece_row in range(4):
        cells = (piece >> (piece_row * 4)) & 0xF
        if cells == 0:
            continue
        board_row = y + piece_row
        shifted = cells << x
        if (
            board_row < 0
            or board_row >= 20
            or shifted > TETRIS_FULL_ROW
            or rows[board_row] & shifted
        ):
            return False
    return True


def lock_tetris_piece(
    rows: tuple[int, ...], piece: int, x: int
) -> tuple[tuple[int, ...], int]:
    if not tetris_piece_valid(rows, piece, x, 0):
        raise SmokeFailure("Tetris bot selected a placement blocked at spawn")
    y = 0
    while tetris_piece_valid(rows, piece, x, y + 1):
        y += 1
    locked = list(rows)
    for piece_row in range(4):
        cells = (piece >> (piece_row * 4)) & 0xF
        if cells:
            locked[y + piece_row] |= cells << x
    remaining = [row for row in locked if row != TETRIS_FULL_ROW]
    cleared = len(locked) - len(remaining)
    return tuple([0] * cleared + remaining), cleared


def infer_spawned_tetris_piece(
    combined: tuple[int, ...], locked: tuple[int, ...]
) -> int:
    if len(combined) != 20 or len(locked) != 20:
        raise SmokeFailure("Tetris board does not have 20 rows")
    active_rows: list[int] = []
    for combined_row, locked_row in zip(combined, locked):
        if (combined_row & locked_row) != locked_row:
            raise SmokeFailure("Tetris frame lost a previously locked cell")
        active_rows.append(combined_row ^ locked_row)
    if sum(row.bit_count() for row in active_rows) != 4:
        raise SmokeFailure("Tetris frame does not contain one four-cell active piece")
    if any(active_rows[4:]):
        raise SmokeFailure("Tetris active piece did not spawn in its four-row mask")
    piece = 0
    for row_number, active_row in enumerate(active_rows[:4]):
        if active_row & 0x7 or active_row >> 7:
            raise SmokeFailure("Tetris active piece did not spawn at x=3")
        piece |= (active_row >> 3) << (row_number * 4)
    if piece.bit_count() != 4:
        raise SmokeFailure("Tetris spawn mask is not a tetromino")
    return piece


def tetris_board_cost(rows: tuple[int, ...], cleared: int) -> int:
    heights: list[int] = []
    holes = 0
    for column in range(10):
        occupied = [row for row in range(20) if rows[row] & (1 << column)]
        if not occupied:
            heights.append(0)
            continue
        top = occupied[0]
        heights.append(20 - top)
        holes += sum(
            1 for row in range(top + 1, 20) if not rows[row] & (1 << column)
        )
    bumpiness = sum(
        abs(left - right) for left, right in zip(heights, heights[1:])
    )
    return (
        holes * 100
        + sum(heights) * 4
        + bumpiness * 2
        + max(heights) * 3
        - cleared * 100
    )


def choose_tetris_placement(
    rows: tuple[int, ...], piece: int
) -> tuple[int, int, tuple[int, ...], int]:
    choices: list[tuple[int, int, int, int, tuple[int, ...], int]] = []
    rotated = piece
    seen: set[int] = set()
    for rotations in range(4):
        if rotated not in seen:
            seen.add(rotated)
            for x in range(10):
                if not tetris_piece_valid(rows, rotated, x, 0):
                    continue
                result, cleared = lock_tetris_piece(rows, rotated, x)
                choices.append(
                    (
                        tetris_board_cost(result, cleared),
                        abs(x - 3) + rotations,
                        rotations,
                        x,
                        result,
                        cleared,
                    )
                )
        rotated = rotate_tetris_piece(rotated)
    if not choices:
        raise SmokeFailure("Tetris bot found no legal placement")
    _, _, rotations, x, result, cleared = min(choices)
    return rotations, x, result, cleared


def send_tetris_key(
    session: QemuSession, screen: TetrisScreen, key: bytes
) -> TetrisScreen:
    session.write(key)
    return wait_for_tetris_screen(session, screen.cursor, timeout=10.0)


def halt(session: QemuSession, cursor: int) -> None:
    session.write(b"halt\r")
    cursor = session.wait_for(b"halt\r\nHalted.\r\n", cursor)
    session.assert_quiet(cursor)


def assert_editor_document(
    session: QemuSession,
    filename: bytes,
    expected: bytes,
    start: int,
    *,
    entered_filename: bytes | None = None,
    enter: bytes = b"\r",
) -> int:
    requested = filename if entered_filename is None else entered_filename
    command = b"edit " + requested
    session.write(command + enter)
    body_start = session.wait_for(
        command + b"\r\n" + editor_frame(filename), start
    )
    session.write(b"\x18")
    end = session.wait_for(b"\r\nnix> ", body_start)
    actual = bytes(session.transcript[body_start:end])
    wanted = expected + b"\r\nnix> "
    if actual != wanted:
        raise SmokeFailure(
            f"editor document mismatch: expected {wanted!r}, found {actual!r}"
        )
    return end


def assert_files(
    session: QemuSession,
    expected: tuple[bytes, ...],
    start: int,
    enter: bytes = b"\r",
) -> int:
    session.write(b"files" + enter)
    if expected:
        listing = b"Files:\r\n" + b"".join(
            normalize_filename(name) + b"\r\n" for name in expected
        )
    else:
        listing = b"No files.\r\n"
    return session.wait_for(b"files\r\n" + listing + b"nix> ", start)


def assert_packages(
    session: QemuSession,
    expected: tuple[bytes, ...],
    start: int,
    enter: bytes = b"\r",
) -> int:
    session.write(b"pkg list" + enter)
    if expected:
        listing = b"Packages:\r\n" + b"".join(
            normalize_filename(name) + b"\r\n" for name in expected
        )
    else:
        listing = b"No packages.\r\n"
    return session.wait_for(b"pkg list\r\n" + listing + b"nix> ", start)


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


def extract_packages(image: bytes) -> tuple[Package, ...]:
    if len(image) != IMAGE_SIZE:
        raise SmokeFailure(
            f"package source image is {len(image)} bytes; expected {IMAGE_SIZE}"
        )
    if PACKAGE_CATALOG_OFFSET != 125 * SECTOR_SIZE:
        raise SmokeFailure("package catalog does not begin at LBA 125")
    if PACKAGE_CATALOG_END != 165 * SECTOR_SIZE:
        raise SmokeFailure("package catalog does not end after LBA 164")

    packages: list[Package] = []
    names: set[bytes] = set()
    found_empty = False
    for slot_index in range(PACKAGE_SLOTS):
        start = PACKAGE_CATALOG_OFFSET + slot_index * PACKAGE_SLOT_SIZE
        slot = image[start : start + PACKAGE_SLOT_SIZE]
        if not any(slot):
            found_empty = True
            continue
        if found_empty:
            raise SmokeFailure("package catalog is not contiguous")

        header = slot[:SECTOR_SIZE]
        payload = slot[SECTOR_SIZE:]
        if header[: len(PACKAGE_SIGNATURE)] != PACKAGE_SIGNATURE:
            raise SmokeFailure(f"package slot {slot_index} has an invalid signature")
        if any(header[14:16]) or any(
            header[PACKAGE_FILENAME_OFFSET + PACKAGE_FILENAME_SIZE :]
        ):
            raise SmokeFailure(
                f"package slot {slot_index} has nonzero reserved header bytes"
            )
        stored_header_checksum = int.from_bytes(
            header[
                PACKAGE_HEADER_CHECKSUM_OFFSET : PACKAGE_HEADER_CHECKSUM_OFFSET + 2
            ],
            "little",
        )
        unchecked_header = bytearray(header)
        unchecked_header[
            PACKAGE_HEADER_CHECKSUM_OFFSET : PACKAGE_HEADER_CHECKSUM_OFFSET + 2
        ] = b"\0\0"
        if checksum16(unchecked_header) != stored_header_checksum:
            raise SmokeFailure(
                f"package slot {slot_index} header checksum is invalid"
            )

        filename_field = header[
            PACKAGE_FILENAME_OFFSET : PACKAGE_FILENAME_OFFSET + PACKAGE_FILENAME_SIZE
        ]
        filename, separator, padding = filename_field.partition(b"\0")
        try:
            normalized = normalize_filename(filename)
        except ValueError as error:
            raise SmokeFailure(
                f"package slot {slot_index} filename is invalid: {error}"
            ) from error
        if (
            not separator
            or padding.strip(b"\0")
            or normalized != filename
            or not filename.endswith(b".BAS")
            or filename in names
        ):
            raise SmokeFailure(f"package slot {slot_index} filename is invalid")

        source_length = int.from_bytes(
            header[PACKAGE_LENGTH_OFFSET : PACKAGE_LENGTH_OFFSET + 2], "little"
        )
        if not 1 <= source_length <= DOCUMENT_MAX:
            raise SmokeFailure(
                f"package {filename.decode('ascii')} length is invalid"
            )
        source = payload[:source_length]
        if (
            b"\0" in source
            or any(value > 0x7F for value in source)
            or b"\n" in source.replace(b"\r\n", b"")
            or b"\r" in source.replace(b"\r\n", b"")
        ):
            raise SmokeFailure(
                f"package {filename.decode('ascii')} source is not canonical ASCII"
            )
        stored_source_checksum = int.from_bytes(
            header[
                PACKAGE_SOURCE_CHECKSUM_OFFSET : PACKAGE_SOURCE_CHECKSUM_OFFSET + 2
            ],
            "little",
        )
        if checksum16(source) != stored_source_checksum:
            raise SmokeFailure(
                f"package {filename.decode('ascii')} source checksum is invalid"
            )
        if any(payload[source_length:]):
            raise SmokeFailure(
                f"package {filename.decode('ascii')} payload padding is not blank"
            )

        names.add(filename)
        packages.append(Package(slot_index, filename, source))

    if not packages:
        raise SmokeFailure("package catalog is empty")
    return tuple(packages)


def find_package(packages: tuple[Package, ...], filename: bytes) -> Package:
    wanted = normalize_filename(filename)
    for package in packages:
        if package.filename == wanted:
            return package
    raise SmokeFailure(f"required package {wanted.decode('ascii')} is missing")


def build_legacy_record(generation: int, document: bytes) -> bytes:
    if len(document) > DOCUMENT_MAX:
        raise ValueError("document exceeds the legacy on-disk format")
    header = bytearray(SECTOR_SIZE)
    header[:4] = LEGACY_STORAGE_MAGIC
    header[4:6] = (generation & 0xFFFF).to_bytes(2, "little")
    header[6:8] = len(document).to_bytes(2, "little")
    header[8:10] = checksum16(document).to_bytes(2, "little")
    header[10:12] = checksum16(header[:10]).to_bytes(2, "little")
    return bytes(header) + document + bytes(FILE_CAPACITY - len(document))


def build_snapshot(
    generation: int, files: tuple[tuple[bytes, bytes], ...]
) -> bytes:
    if len(files) > MAX_FILES:
        raise ValueError("snapshot has too many files")
    header = bytearray(SECTOR_SIZE)
    payloads = bytearray(MAX_FILES * FILE_CAPACITY)
    header[:4] = STORAGE_MAGIC
    header[4:6] = (generation & 0xFFFF).to_bytes(2, "little")
    header[6] = len(files)
    names: set[bytes] = set()
    for index, (filename, document) in enumerate(files):
        name = normalize_filename(filename)
        if name in names:
            raise ValueError("snapshot has duplicate filenames")
        if len(document) > DOCUMENT_MAX:
            raise ValueError("document exceeds the on-disk format")
        names.add(name)
        entry = ENTRY_OFFSET + index * ENTRY_SIZE
        header[entry : entry + len(name)] = name
        header[entry + ENTRY_LENGTH_OFFSET : entry + ENTRY_LENGTH_OFFSET + 2] = (
            len(document).to_bytes(2, "little")
        )
        header[
            entry + ENTRY_CHECKSUM_OFFSET : entry + ENTRY_CHECKSUM_OFFSET + 2
        ] = checksum16(document).to_bytes(2, "little")
        payload = index * FILE_CAPACITY
        payloads[payload : payload + len(document)] = document
    header[HEADER_CHECKSUM_OFFSET : HEADER_CHECKSUM_OFFSET + 2] = checksum16(
        header[:HEADER_CHECKSUM_OFFSET]
    ).to_bytes(2, "little")
    return bytes(header + payloads)


def install_snapshot(image: bytearray, slot: int, snapshot: bytes) -> None:
    if len(snapshot) != SNAPSHOT_SIZE:
        raise ValueError("file snapshot has the wrong size")
    start = SNAPSHOT_OFFSETS[slot]
    image[start : start + SNAPSHOT_SIZE] = snapshot


def install_legacy_record(image: bytearray, slot: int, record: bytes) -> None:
    if len(record) != 5 * SECTOR_SIZE:
        raise ValueError("legacy record has the wrong size")
    start = SNAPSHOT_OFFSETS[slot]
    image[start : start + len(record)] = record


def parse_snapshot(
    data: bytes, slot: int
) -> tuple[int, tuple[tuple[bytes, bytes], ...]] | None:
    snapshot_offset = SNAPSHOT_OFFSETS[slot]
    header = data[snapshot_offset : snapshot_offset + SECTOR_SIZE]
    if header[:4] != STORAGE_MAGIC or header[7] != 0:
        return None
    generation = int.from_bytes(header[4:6], "little")
    file_count = header[6]
    if file_count > MAX_FILES:
        return None
    expected_header_checksum = int.from_bytes(
        header[HEADER_CHECKSUM_OFFSET : HEADER_CHECKSUM_OFFSET + 2], "little"
    )
    if expected_header_checksum != checksum16(header[:HEADER_CHECKSUM_OFFSET]):
        return None

    files: list[tuple[bytes, bytes]] = []
    names: set[bytes] = set()
    for index in range(file_count):
        entry = ENTRY_OFFSET + index * ENTRY_SIZE
        name_field = header[entry : entry + FILENAME_SIZE]
        nul = name_field.find(b"\0")
        if nul <= 0 or any(name_field[nul + 1 :]):
            return None
        name = name_field[:nul]
        try:
            if normalize_filename(name) != name:
                return None
        except ValueError:
            return None
        if name in names or header[entry + FILENAME_SIZE] != 0:
            return None
        length = int.from_bytes(
            header[
                entry + ENTRY_LENGTH_OFFSET : entry + ENTRY_LENGTH_OFFSET + 2
            ],
            "little",
        )
        if length > DOCUMENT_MAX:
            return None
        payload = snapshot_offset + SECTOR_SIZE + index * FILE_CAPACITY
        document = data[payload : payload + length]
        expected_file_checksum = int.from_bytes(
            header[
                entry + ENTRY_CHECKSUM_OFFSET : entry + ENTRY_CHECKSUM_OFFSET + 2
            ],
            "little",
        )
        if expected_file_checksum != checksum16(document):
            return None
        names.add(name)
        files.append((name, document))
    return generation, tuple(files)


def newest_snapshot(
    data: bytes,
) -> tuple[int, int, tuple[tuple[bytes, bytes], ...]] | None:
    snapshot_a = parse_snapshot(data, 0)
    snapshot_b = parse_snapshot(data, 1)
    if snapshot_a is None and snapshot_b is None:
        return None
    if snapshot_b is None:
        assert snapshot_a is not None
        generation, files = snapshot_a
        return 0, generation, files
    if snapshot_a is None:
        generation, files = snapshot_b
        return 1, generation, files

    generation_a, files_a = snapshot_a
    generation_b, files_b = snapshot_b
    delta = (generation_a - generation_b) & 0xFFFF
    if delta <= 0x8000:
        return 0, generation_a, files_a
    return 1, generation_b, files_b


def assert_saved_snapshot(
    image: Path,
    expected: tuple[tuple[bytes, bytes], ...],
    template: bytes,
    *,
    expected_slot: int,
    expected_generation: int,
) -> bytes:
    data = image.read_bytes()
    if len(data) != IMAGE_SIZE:
        raise SmokeFailure(f"runtime image changed size to {len(data)} bytes")
    if data[:STORAGE_OFFSET] != template[:STORAGE_OFFSET]:
        raise SmokeFailure("saving files changed boot or kernel sectors")
    if data[STORAGE_END:] != template[STORAGE_END:]:
        raise SmokeFailure("saving files changed the printer module or unused sectors")

    normalized = tuple(
        (normalize_filename(filename), document) for filename, document in expected
    )
    newest = newest_snapshot(data)
    wanted = (expected_slot, expected_generation, normalized)
    if newest != wanted:
        raise SmokeFailure(f"newest snapshot is {newest!r}; expected {wanted!r}")

    for slot in range(2):
        snapshot = parse_snapshot(data, slot)
        if snapshot is None:
            continue
        _, files = snapshot
        snapshot_offset = SNAPSHOT_OFFSETS[slot]
        header = data[snapshot_offset : snapshot_offset + SECTOR_SIZE]
        unused_entries = header[
            ENTRY_OFFSET + len(files) * ENTRY_SIZE : HEADER_CHECKSUM_OFFSET
        ]
        if any(unused_entries) or any(header[HEADER_CHECKSUM_OFFSET + 2 :]):
            raise SmokeFailure(f"snapshot {slot} retained unused header bytes")
        for index, (_, document) in enumerate(files):
            payload = snapshot_offset + SECTOR_SIZE + index * FILE_CAPACITY
            if any(data[payload + len(document) : payload + FILE_CAPACITY]):
                raise SmokeFailure(
                    f"snapshot {slot} file {index} retained deleted content"
                )
        unused_payload = (
            snapshot_offset + SECTOR_SIZE + len(files) * FILE_CAPACITY
        )
        if any(data[unused_payload : snapshot_offset + SNAPSHOT_SIZE]):
            raise SmokeFailure(f"snapshot {slot} retained unused file payloads")
    return data


def assert_files_on_boot(
    qemu: str,
    image: Path,
    expected: tuple[tuple[bytes, bytes], ...],
) -> None:
    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        cursor = assert_files(
            session, tuple(filename for filename, _ in expected), cursor
        )
        for filename, document in expected:
            cursor = assert_editor_document(session, filename, document, cursor)
        halt(session, cursor)


def exercise_basic(qemu: str, image: Path, template: bytes) -> None:
    filename = b"PROGRAM.BAS"
    entered = b"program.bas"
    program = (
        b"10 rem keywords are case insensitive\r\n"
        b"20 print a\r\n"
        b"30 let a = -2\r\n"
        b"40 print \"COUNT\"\r\n"
        b"50 print a\r\n"
        b"60 let a = a + 1\r\n"
        b"70 if a < 1 then 50\r\n"
        b"80 if a = 1 then 100\r\n"
        b"90 print \"BAD\"\r\n"
        b"100 if a > 0 then 120\r\n"
        b"110 print \"BAD\"\r\n"
        b"120 goto 140\r\n"
        b"130 print \"BAD\"\r\n"
        b"140 end"
    )
    output = b"0\r\nCOUNT\r\n-2\r\n-1\r\n0\r\n"
    successful_run = BASIC_FRAME + output + BASIC_FINISHED
    original = image.read_bytes()

    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        cursor = assert_files(session, (), cursor)
        command = b"edit " + entered
        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(filename), cursor
        )
        session.write(program + b"\x12")
        cursor = session.wait_for(program + successful_run, cursor)
        session.write(b" ")
        cursor = session.wait_for(editor_frame(filename) + program, cursor)
        session.write(b"\x18")
        cursor = session.wait_for(b"\r\nnix> ", cursor)
        cursor = assert_files(session, (), cursor)
        halt(session, cursor)
    if image.read_bytes() != original:
        raise SmokeFailure("running an unsaved BASIC file changed the disk image")

    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        command = b"edit " + entered
        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(filename), cursor
        )
        session.write(program + b"\x13")
        cursor = session.wait_for(
            program + editor_frame(filename, b"Saved.") + program, cursor
        )
        session.write(b"\x12")
        cursor = session.wait_for(successful_run, cursor)
        session.write(b" ")
        cursor = session.wait_for(editor_frame(filename) + program, cursor)
        session.write(b"\x18reboot\r")
        cursor = session.wait_for(
            b"\r\nnix> reboot\r\nRebooting...\r\n", cursor
        )
        cursor = session.wait_for(
            b"Nixodria OS\r\nType help.\r\nnix> ", cursor
        )
        cursor = assert_files(session, (filename,), cursor)

        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(filename) + program, cursor
        )
        session.write(b"\x12")
        cursor = session.wait_for(successful_run, cursor)
        session.write(b" ")
        cursor = session.wait_for(editor_frame(filename) + program, cursor)

        missing_line = b'10 print "BEFORE"\r\n20 goto 999'
        session.write(b"\x0c")
        cursor = session.wait_for(editor_frame(filename), cursor)
        session.write(missing_line + b"\x12")
        cursor = session.wait_for(
            missing_line
            + BASIC_FRAME
            + b"BEFORE\r\n\r\nBASIC error at line 20. Press any key.",
            cursor,
        )
        session.write(b"\r\n")
        cursor = session.wait_for(editor_frame(filename) + missing_line, cursor)

        overflow_program = b"10 print 65536"
        session.write(b"\x0c")
        cursor = session.wait_for(editor_frame(filename), cursor)
        session.write(overflow_program + b"\x12")
        cursor = session.wait_for(
            overflow_program
            + BASIC_FRAME
            + b"\r\nBASIC error at line 10. Press any key.",
            cursor,
        )
        session.write(b" ")
        cursor = session.wait_for(
            editor_frame(filename) + overflow_program, cursor
        )
        session.write(b"\x18reboot\r")
        cursor = session.wait_for(
            b"\r\nnix> reboot\r\nRebooting...\r\n", cursor
        )
        cursor = session.wait_for(
            b"Nixodria OS\r\nType help.\r\nnix> ", cursor
        )

        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(filename) + program, cursor
        )
        guard_program = b"10 goto 10"
        session.write(b"\x0c")
        cursor = session.wait_for(editor_frame(filename), cursor)
        session.write(guard_program + b"\x12")
        cursor = session.wait_for(
            guard_program
            + BASIC_FRAME
            + b"\r\nBASIC error at line 10. Press any key.",
            cursor,
            timeout=10.0,
        )
        session.write(b" ")
        cursor = session.wait_for(editor_frame(filename) + guard_program, cursor)
        session.write(b"\x18reboot\r")
        cursor = session.wait_for(
            b"\r\nnix> reboot\r\nRebooting...\r\n", cursor
        )
        cursor = session.wait_for(
            b"Nixodria OS\r\nType help.\r\nnix> ", cursor
        )
        cursor = assert_editor_document(
            session,
            filename,
            program,
            cursor,
            entered_filename=b"PrOgRaM.BaS",
        )
        halt(session, cursor)

    assert_saved_snapshot(
        image,
        ((filename, program),),
        template,
        expected_slot=0,
        expected_generation=0,
    )


def exercise_extended_basic(qemu: str, image: Path) -> None:
    filename = b"EXTENDED.BAS"
    program = (
        b'10 cls:print "EXTENDED"\r\n'
        b"20 dim a(3):a(1)=6:let a(2)=5\r\n"
        b"30 x=(a(1)+a(2))*6/2:y=x mod 10:z=x and 7\r\n"
        b'40 print "MATH ";:print x;:print ",";:print y;:print ",";:print z\r\n'
        b'50 c=1:gosub 200:c=c+10:print "RETURN ";:print c\r\n'
        b'60 timer t:wait 1:timer u:print "YIELD"\r\n'
        b"70 wait 1:key k:if k=0 then 70\r\n"
        b'80 print "KEY ";:print k\r\n'
        b"90 end\r\n"
        b"200 c=c+2:return"
    )
    original = image.read_bytes()

    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        command = b"edit " + filename
        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(filename), cursor
        )
        session.write(program + b"\x12")
        cursor = session.wait_for(
            program
            + BASIC_FRAME
            + CLEAR_SCREEN
            + b"EXTENDED\r\n"
            + b"MATH 33,3,1\r\n"
            + b"RETURN 13\r\n"
            + b"YIELD\r\n",
            cursor,
            timeout=10.0,
        )
        session.write(b"Z")
        cursor = session.wait_for(b"KEY 90\r\n" + BASIC_FINISHED, cursor)
        session.write(b" ")
        cursor = session.wait_for(editor_frame(filename) + program, cursor)
        session.write(b"\x18")
        cursor = session.wait_for(b"\r\nnix> ", cursor)
        cursor = assert_files(session, (), cursor)
        halt(session, cursor)

    if image.read_bytes() != original:
        raise SmokeFailure("extended unsaved BASIC checks changed the disk image")


def exercise_corrupt_basic_module(
    qemu: str, image: Path, tetris_source: bytes
) -> None:
    damaged = bytearray(image.read_bytes())
    install_snapshot(
        damaged,
        0,
        build_snapshot(0, ((b"TETRIS.BAS", tetris_source),)),
    )
    damaged[BASIC_MODULE_OFFSET + 128] ^= 0x5A
    image.write_bytes(damaged)
    damaged_before = image.read_bytes()

    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        session.write(b"run TETRIS.BAS\r")
        cursor = session.wait_for(
            b"run TETRIS.BAS\r\n"
            + CLEAR_SCREEN
            + b"BASIC runtime unavailable. Press any key.",
            cursor,
        )
        session.write(b" ")
        cursor = session.wait_for(b"nix> ", cursor)
        session.write(b"echo OK\r")
        cursor = session.wait_for(b"echo OK\r\nOK\r\nnix> ", cursor)
        halt(session, cursor)

    if image.read_bytes() != damaged_before:
        raise SmokeFailure("corrupt BASIC-module refusal changed the disk image")


def exercise_package_tetris(
    qemu: str,
    image: Path,
    template: bytes,
    packages: tuple[Package, ...],
) -> None:
    package = find_package(packages, b"TETRIS.BAS")
    source = package.source
    package_names = tuple(item.filename for item in packages)

    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        cursor = assert_packages(session, package_names, cursor)
        cursor = assert_files(session, (), cursor)

        session.write(b"run TETRIS.BAS\r")
        cursor = session.wait_for(
            b"run TETRIS.BAS\r\nFile not found.\r\nnix> ", cursor
        )

        install_command = b"pkg install tEtRiS.bAs"
        session.write(install_command + b"\r")
        cursor = session.wait_for(
            install_command + b"\r\nPackage installed.\r\nnix> ", cursor
        )
        cursor = assert_files(session, (package.filename,), cursor)

        session.write(b"reboot\r")
        cursor = session.wait_for(b"reboot\r\nRebooting...\r\n", cursor)
        cursor = session.wait_for(
            b"Nixodria OS\r\nType help.\r\nnix> ", cursor
        )
        cursor = assert_packages(session, package_names, cursor)
        cursor = assert_files(session, (package.filename,), cursor)
        cursor = assert_editor_document(
            session,
            package.filename,
            source,
            cursor,
            entered_filename=b"tetris.bas",
        )

        session.write(b"run TETRIS.BAS\r")
        cursor = session.wait_for(b"run TETRIS.BAS\r\n" + BASIC_FRAME, cursor)
        screen = wait_for_tetris_screen(session, cursor, timeout=10.0)
        if screen.game_over or tetris_occupied_cells(screen) != 4:
            raise SmokeFailure("Tetris initial frame does not have one active piece")
        if screen.score != 0 or screen.lines != 0:
            raise SmokeFailure("Tetris initial score or line count is not zero")
        initial_piece = infer_spawned_tetris_piece(screen.rows, (0,) * 20)

        left = send_tetris_key(session, screen, b"a")
        if left.game_over or left.rows == screen.rows or tetris_occupied_cells(left) != 4:
            raise SmokeFailure("Tetris left movement did not redraw the active piece")
        right = send_tetris_key(session, left, b"d")
        if right.game_over or right.rows == left.rows or tetris_occupied_cells(right) != 4:
            raise SmokeFailure("Tetris right movement did not redraw the active piece")
        rotated = send_tetris_key(session, right, b"w")
        if rotated.game_over or tetris_occupied_cells(rotated) != 4:
            raise SmokeFailure("Tetris rotation did not return a valid frame")
        lowered = send_tetris_key(session, rotated, b"s")
        if (
            lowered.game_over
            or lowered.rows == rotated.rows
            or tetris_occupied_cells(lowered) != 4
        ):
            raise SmokeFailure("Tetris soft drop did not redraw a lower active piece")

        first_orientation = rotate_tetris_piece(initial_piece)
        locked, first_cleared = lock_tetris_piece(
            (0,) * 20, first_orientation, 3
        )
        if first_cleared:
            raise SmokeFailure("the first Tetris piece unexpectedly cleared a line")
        screen = send_tetris_key(session, lowered, b" ")
        if screen.game_over or tetris_occupied_cells(screen) != 8:
            raise SmokeFailure(
                "the first Tetris hard drop did not leave four locked and four active cells"
            )
        infer_spawned_tetris_piece(screen.rows, locked)

        cleared_a_line = False
        for _ in range(40):
            piece = infer_spawned_tetris_piece(screen.rows, locked)
            rotations, x, expected_locked, cleared = choose_tetris_placement(
                locked, piece
            )
            for _ in range(rotations):
                screen = send_tetris_key(session, screen, b"w")
                if screen.game_over:
                    raise SmokeFailure("Tetris ended while rotating a legal placement")
            move = b"a" if x < 3 else b"d"
            for _ in range(abs(x - 3)):
                screen = send_tetris_key(session, screen, move)
                if screen.game_over:
                    raise SmokeFailure("Tetris ended while moving a legal placement")
            previous_score = screen.score
            previous_lines = screen.lines
            screen = send_tetris_key(session, screen, b" ")
            if screen.game_over:
                raise SmokeFailure("Tetris ended before the line-clear assertion")
            if screen.lines != previous_lines + cleared:
                raise SmokeFailure(
                    "Tetris line counter did not match simulated compaction"
                )
            if screen.score != previous_score + cleared * 100:
                raise SmokeFailure("Tetris score did not match cleared lines")
            locked = expected_locked
            infer_spawned_tetris_piece(screen.rows, locked)
            if cleared:
                cleared_a_line = True
                break
        if not cleared_a_line:
            raise SmokeFailure("Tetris bot did not produce a line clear")

        for _ in range(64):
            screen = send_tetris_key(session, screen, b" ")
            if screen.game_over:
                break
        else:
            raise SmokeFailure("repeated Tetris hard drops did not reach game over")

        session.write(b"r")
        screen = wait_for_tetris_screen(session, screen.cursor, timeout=10.0)
        if (
            screen.game_over
            or tetris_occupied_cells(screen) != 4
            or screen.score != 0
            or screen.lines != 0
        ):
            raise SmokeFailure("Tetris restart did not reset to one active piece")

        session.write(b"q")
        cursor = session.wait_for(BASIC_FINISHED, screen.cursor, timeout=10.0)
        session.write(b" ")
        cursor = session.wait_for(b"nix> ", cursor)
        session.write(b"echo OK\r")
        cursor = session.wait_for(b"echo OK\r\nOK\r\nnix> ", cursor)
        cursor = assert_files(session, (package.filename,), cursor)
        halt(session, cursor)

    assert_saved_snapshot(
        image,
        ((package.filename, source),),
        template,
        expected_slot=0,
        expected_generation=0,
    )


def exercise_package_lifecycle(
    qemu: str,
    image: Path,
    template: bytes,
    packages: tuple[Package, ...],
) -> None:
    package = find_package(packages, b"HELLO.BAS")
    filename = package.filename
    source = package.source
    override = b'10 print "OVERRIDE"\r\n20 end'
    package_names = tuple(item.filename for item in packages)

    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)

        session.write(b"pkg install MISSING.BAS\r")
        cursor = session.wait_for(
            b"pkg install MISSING.BAS\r\nPackage not found.\r\nnix> ", cursor
        )
        session.write(b"pkg remove missing.bas\r")
        cursor = session.wait_for(
            b"pkg remove missing.bas\r\nPackage is not installed.\r\nnix> ",
            cursor,
        )
        session.write(b"pkg remove TETRIS.BAS\r")
        cursor = session.wait_for(
            b"pkg remove TETRIS.BAS\r\nPackage is not installed.\r\nnix> ",
            cursor,
        )

        session.write(b"pkg install HELLO.BAS\r")
        cursor = session.wait_for(
            b"pkg install HELLO.BAS\r\nPackage installed.\r\nnix> ", cursor
        )
        cursor = assert_editor_document(session, filename, source, cursor)

        command = b"edit " + filename
        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(filename) + source, cursor
        )
        session.write(b"\x0c")
        cursor = session.wait_for(editor_frame(filename), cursor)
        session.write(override + b"\x13")
        cursor = session.wait_for(
            override + editor_frame(filename, b"Saved.") + override, cursor
        )
        session.write(b"\x18")
        cursor = session.wait_for(b"\r\nnix> ", cursor)

        session.write(b"pkg install hElLo.BaS\r")
        cursor = session.wait_for(
            b"pkg install hElLo.BaS\r\n"
            b"Package already installed.\r\nnix> ",
            cursor,
        )
        cursor = assert_editor_document(session, filename, override, cursor)

        session.write(b"run HELLO.BAS\r")
        cursor = session.wait_for(
            b"run HELLO.BAS\r\n"
            + BASIC_FRAME
            + b"OVERRIDE\r\n"
            + BASIC_FINISHED,
            cursor,
        )
        session.write(b" ")
        cursor = session.wait_for(b"nix> ", cursor)
        cursor = assert_editor_document(session, filename, override, cursor)

        session.write(b"pkg remove hello.bas\r")
        cursor = session.wait_for(
            b"pkg remove hello.bas\r\nPackage removed.\r\nnix> ", cursor
        )
        cursor = assert_files(session, (), cursor)
        session.write(b"pkg remove HELLO.BAS\r")
        cursor = session.wait_for(
            b"pkg remove HELLO.BAS\r\nPackage is not installed.\r\nnix> ",
            cursor,
        )

        session.write(b"reboot\r")
        cursor = session.wait_for(b"reboot\r\nRebooting...\r\n", cursor)
        cursor = session.wait_for(
            b"Nixodria OS\r\nType help.\r\nnix> ", cursor
        )
        cursor = assert_packages(session, package_names, cursor)
        cursor = assert_files(session, (), cursor)
        session.write(b"run HELLO.BAS\r")
        cursor = session.wait_for(
            b"run HELLO.BAS\r\nFile not found.\r\nnix> ", cursor
        )
        halt(session, cursor)

    assert_saved_snapshot(
        image,
        (),
        template,
        expected_slot=0,
        expected_generation=2,
    )

    retired_template = bytearray(template)
    retired_slot = PACKAGE_CATALOG_OFFSET + package.slot * PACKAGE_SLOT_SIZE
    retired_template[retired_slot : retired_slot + PACKAGE_SLOT_SIZE] = bytes(
        PACKAGE_SLOT_SIZE
    )
    install_snapshot(
        retired_template,
        0,
        build_snapshot(4, ((filename, source),)),
    )
    retired_image = image.with_name("retired-package-removal.img")
    retired_image.write_bytes(retired_template)
    with QemuSession(qemu, retired_image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        cursor = assert_packages(session, (b"TETRIS.BAS",), cursor)
        cursor = assert_files(session, (filename,), cursor)
        session.write(b"pkg remove HELLO.BAS\r")
        cursor = session.wait_for(
            b"pkg remove HELLO.BAS\r\nPackage removed.\r\nnix> ", cursor
        )
        cursor = assert_files(session, (), cursor)
        halt(session, cursor)
    assert_saved_snapshot(
        retired_image,
        (),
        bytes(retired_template),
        expected_slot=1,
        expected_generation=5,
    )


def exercise_package_compaction(
    qemu: str,
    image: Path,
    template: bytes,
    packages: tuple[Package, ...],
) -> None:
    tetris = find_package(packages, b"TETRIS.BAS")
    hello = find_package(packages, b"HELLO.BAS")
    notes_name = b"NOTES.TXT"
    notes = b"kept after package removal"

    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        for package in (tetris, hello):
            command = b"pkg install " + package.filename
            session.write(command + b"\r")
            cursor = session.wait_for(
                command + b"\r\nPackage installed.\r\nnix> ", cursor
            )

        command = b"edit " + notes_name
        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(notes_name), cursor
        )
        session.write(notes + b"\x13")
        cursor = session.wait_for(
            notes + editor_frame(notes_name, b"Saved.") + notes, cursor
        )
        session.write(b"\x18")
        cursor = session.wait_for(b"\r\nnix> ", cursor)
        cursor = assert_files(
            session, (tetris.filename, hello.filename, notes_name), cursor
        )

        session.write(b"pkg remove HELLO.BAS\r")
        cursor = session.wait_for(
            b"pkg remove HELLO.BAS\r\nPackage removed.\r\nnix> ", cursor
        )
        expected_names = (tetris.filename, notes_name)
        cursor = assert_files(session, expected_names, cursor)
        cursor = assert_editor_document(
            session, tetris.filename, tetris.source, cursor
        )
        cursor = assert_editor_document(session, notes_name, notes, cursor)

        session.write(b"reboot\r")
        cursor = session.wait_for(b"reboot\r\nRebooting...\r\n", cursor)
        cursor = session.wait_for(
            b"Nixodria OS\r\nType help.\r\nnix> ", cursor
        )
        cursor = assert_files(session, expected_names, cursor)
        cursor = assert_editor_document(
            session, tetris.filename, tetris.source, cursor
        )
        cursor = assert_editor_document(session, notes_name, notes, cursor)
        halt(session, cursor)

    assert_saved_snapshot(
        image,
        ((tetris.filename, tetris.source), (notes_name, notes)),
        template,
        expected_slot=1,
        expected_generation=3,
    )


def exercise_corrupt_package_catalog(
    qemu: str,
    template: bytes,
    directory: Path,
    packages: tuple[Package, ...],
) -> None:
    package = find_package(packages, b"TETRIS.BAS")
    package_names = tuple(item.filename for item in packages)
    slot_offset = PACKAGE_CATALOG_OFFSET + package.slot * PACKAGE_SLOT_SIZE

    zeroed_header = bytearray(template)
    zeroed_header[slot_offset : slot_offset + SECTOR_SIZE] = bytes(SECTOR_SIZE)
    zeroed_header_image = directory / "zeroed-package-header.img"
    zeroed_header_image.write_bytes(zeroed_header)
    zeroed_header_before = zeroed_header_image.read_bytes()
    with QemuSession(qemu, zeroed_header_image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        session.write(b"pkg list\r")
        cursor = session.wait_for(
            b"pkg list\r\nPackage catalog unavailable.\r\nnix> ", cursor
        )
        session.write(b"pkg install TETRIS.BAS\r")
        cursor = session.wait_for(
            b"pkg install TETRIS.BAS\r\n"
            b"Package catalog unavailable.\r\nnix> ",
            cursor,
        )
        cursor = assert_files(session, (), cursor)
        halt(session, cursor)
    if zeroed_header_image.read_bytes() != zeroed_header_before:
        raise SmokeFailure("zeroed package-header refusal changed the disk image")

    blank_slot = bytearray(template)
    blank_slot[slot_offset : slot_offset + PACKAGE_SLOT_SIZE] = bytes(
        PACKAGE_SLOT_SIZE
    )
    blank_slot_image = directory / "blank-package-slot-hole.img"
    blank_slot_image.write_bytes(blank_slot)
    blank_slot_before = blank_slot_image.read_bytes()
    with QemuSession(qemu, blank_slot_image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        session.write(b"pkg list\r")
        cursor = session.wait_for(
            b"pkg list\r\nPackage catalog unavailable.\r\nnix> ", cursor
        )
        session.write(b"pkg install HELLO.BAS\r")
        cursor = session.wait_for(
            b"pkg install HELLO.BAS\r\n"
            b"Package catalog unavailable.\r\nnix> ",
            cursor,
        )
        cursor = assert_files(session, (), cursor)
        halt(session, cursor)
    if blank_slot_image.read_bytes() != blank_slot_before:
        raise SmokeFailure("package-slot hole refusal changed the disk image")

    bad_header = bytearray(template)
    bad_header[slot_offset + PACKAGE_HEADER_CHECKSUM_OFFSET] ^= 0x01
    header_image = directory / "corrupt-package-header.img"
    header_image.write_bytes(bad_header)
    header_before = header_image.read_bytes()
    with QemuSession(qemu, header_image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        session.write(b"pkg list\r")
        cursor = session.wait_for(
            b"pkg list\r\nPackage catalog unavailable.\r\nnix> ", cursor
        )
        session.write(b"pkg install TETRIS.BAS\r")
        cursor = session.wait_for(
            b"pkg install TETRIS.BAS\r\n"
            b"Package catalog unavailable.\r\nnix> ",
            cursor,
        )
        cursor = assert_files(session, (), cursor)
        halt(session, cursor)
    if header_image.read_bytes() != header_before:
        raise SmokeFailure("corrupt package-header refusal changed the disk image")

    bad_payload = bytearray(template)
    bad_payload[slot_offset + SECTOR_SIZE] ^= 0x01
    payload_image = directory / "corrupt-package-payload.img"
    payload_image.write_bytes(bad_payload)
    payload_before = payload_image.read_bytes()
    with QemuSession(qemu, payload_image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        cursor = assert_packages(session, package_names, cursor)
        session.write(b"pkg install TETRIS.BAS\r")
        cursor = session.wait_for(
            b"pkg install TETRIS.BAS\r\nPackage install failed.\r\nnix> ",
            cursor,
        )
        cursor = assert_files(session, (), cursor)
        session.write(b"run TETRIS.BAS\r")
        cursor = session.wait_for(
            b"run TETRIS.BAS\r\nFile not found.\r\nnix> ", cursor
        )
        halt(session, cursor)
    if payload_image.read_bytes() != payload_before:
        raise SmokeFailure("corrupt package-payload refusal changed the disk image")


def exercise_package_write_failures(
    qemu: str,
    template: bytes,
    directory: Path,
    packages: tuple[Package, ...],
) -> None:
    package = find_package(packages, b"HELLO.BAS")

    install_image = directory / "readonly-package-install.img"
    install_image.write_bytes(template)
    before_install = install_image.read_bytes()
    with QemuSession(qemu, install_image, readonly=True) as session:
        cursor = session.wait_for(b"nix> ", 0)
        session.write(b"pkg install HELLO.BAS\r")
        cursor = session.wait_for(
            b"pkg install HELLO.BAS\r\nPackage install failed.\r\nnix> ",
            cursor,
        )
        cursor = assert_files(session, (), cursor)
        halt(session, cursor)
    if install_image.read_bytes() != before_install:
        raise SmokeFailure("failed package install changed a read-only image")
    assert_files_on_boot(qemu, install_image, ())

    installed = bytearray(template)
    install_snapshot(
        installed,
        0,
        build_snapshot(4, ((package.filename, package.source),)),
    )
    remove_image = directory / "readonly-package-remove.img"
    remove_image.write_bytes(installed)
    before_remove = remove_image.read_bytes()
    expected = ((package.filename, package.source),)
    with QemuSession(qemu, remove_image, readonly=True) as session:
        cursor = session.wait_for(b"nix> ", 0)
        session.write(b"pkg remove hello.bas\r")
        cursor = session.wait_for(
            b"pkg remove hello.bas\r\nPackage remove failed.\r\nnix> ",
            cursor,
        )
        cursor = assert_files(session, (package.filename,), cursor)
        cursor = assert_editor_document(
            session, package.filename, package.source, cursor
        )
        halt(session, cursor)
    if remove_image.read_bytes() != before_remove:
        raise SmokeFailure("failed package removal changed a read-only image")
    assert_files_on_boot(qemu, remove_image, expected)

    keep_name = b"KEEP.TXT"
    keep_document = b"preserved beside failed package removal"
    rollback_files = (
        (package.filename, package.source),
        (keep_name, keep_document),
    )
    rollback_data = bytearray(template)
    install_snapshot(rollback_data, 0, build_snapshot(4, rollback_files))
    rollback_image = directory / "fault-package-remove-rollback.img"
    rollback_image.write_bytes(rollback_data)
    rollback_config = directory / "fault-package-remove-rollback.conf"

    # The first write invalidates snapshot B and advances blkdebug to state 2.
    # Three one-shot rules then fail every BIOS retry of the final header write.
    # Snapshot reads remain faulted in that state, so disk-based rollback loses
    # the live directory while an in-memory rollback can continue safely.
    state_rule = """
[set-state]
event = "write_aio"
state = "1"
new_state = "2"
"""
    write_failure_rule = f"""
[inject-error]
event = "write_aio"
state = "2"
errno = "5"
sector = "{SNAPSHOT_LBAS[1]}"
once = "on"
iotype = "write"
"""
    read_failure_rules = tuple(
        f"""
[inject-error]
event = "read_aio"
state = "2"
errno = "5"
sector = "{sector}"
iotype = "read"
"""
        for sector in SNAPSHOT_LBAS
    )
    rollback_config.write_text(
        "\n\n".join(
            rule.strip()
            for rule in (
                state_rule,
                write_failure_rule,
                write_failure_rule,
                write_failure_rule,
                *read_failure_rules,
            )
        )
        + "\n",
        encoding="utf-8",
    )

    after_name = b"AFTER.TXT"
    after_document = b"saved after failed package removal"
    with QemuSession(qemu, rollback_image, blkdebug_config=rollback_config) as session:
        cursor = session.wait_for(b"nix> ", 0)
        session.write(b"pkg remove HELLO.BAS\r")
        cursor = session.wait_for(
            b"pkg remove HELLO.BAS\r\nPackage remove failed.\r\nnix> ",
            cursor,
            timeout=10.0,
        )
        cursor = assert_files(
            session, tuple(filename for filename, _ in rollback_files), cursor
        )
        for filename, document in rollback_files:
            cursor = assert_editor_document(session, filename, document, cursor)

        command = b"edit " + after_name
        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(after_name), cursor
        )
        session.write(after_document + b"\x13")
        cursor = session.wait_for(
            after_document
            + editor_frame(after_name, b"Saved.")
            + after_document,
            cursor,
            timeout=10.0,
        )
        session.write(b"\x18")
        cursor = session.wait_for(b"\r\nnix> ", cursor)
        halt(session, cursor)

    after_files = rollback_files + ((after_name, after_document),)
    assert_saved_snapshot(
        rollback_image,
        after_files,
        template,
        expected_slot=1,
        expected_generation=5,
    )
    assert_files_on_boot(qemu, rollback_image, after_files)


def exercise_filename_rules(qemu: str, image: Path) -> None:
    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        session.write(b"help\r\n")
        cursor = session.wait_for(
            b"help\r\n"
            b"help files edit [filename] run <filename>\r\n"
            b"pkg list pkg install <filename> pkg remove <filename>\r\n"
            b"print <filename> printer [IPv4] clear echo <text> reboot halt\r\n"
            b"nix> ",
            cursor,
        )
        cursor = assert_files(session, (), cursor)

        session.write(b"printer\r")
        cursor = session.wait_for(
            b"printer\r\nPrinter: 192.168.40.220\r\nnix> ", cursor
        )
        for invalid_address in (
            b"1.2.3",
            b"1.2.3.4.5",
            b"256.2.3.4",
            b"1.2.x.4",
        ):
            command = b"printer " + invalid_address
            session.write(command + b"\r")
            cursor = session.wait_for(
                command + b"\r\nInvalid IPv4 address.\r\nnix> ", cursor
            )
        session.write(b"printer 10.0.2.100\r")
        cursor = session.wait_for(
            b"printer 10.0.2.100\r\nPrinter configured.\r\nnix> ", cursor
        )
        session.write(b"printer\r")
        cursor = session.wait_for(
            b"printer\r\nPrinter: 10.0.2.100\r\nnix> ", cursor
        )
        session.write(b"print missing.txt\r")
        cursor = session.wait_for(
            b"print missing.txt\r\nFile not found.\r\nnix> ", cursor
        )
        session.write(b"print bad/name\r")
        cursor = session.wait_for(
            b"print bad/name\r\nInvalid filename.\r\nnix> ", cursor
        )

        for invalid in (b"bad/name", b"has space", b"x" * 13):
            command = b"edit " + invalid
            session.write(command + b"\r")
            cursor = session.wait_for(
                command + b"\r\nInvalid filename.\r\nnix> ", cursor
            )
            for prefix in (b"pkg install ", b"pkg remove "):
                command = prefix + invalid
                session.write(command + b"\r")
                cursor = session.wait_for(
                    command + b"\r\nInvalid filename.\r\nnix> ", cursor
                )

        valid = b"a_b-c.d12345"
        command = b"edit " + valid
        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(valid), cursor
        )
        session.write(b"not saved\x18")
        cursor = session.wait_for(b"not saved\r\nnix> ", cursor)
        cursor = assert_files(session, (), cursor)

        session.write(b"edit\r")
        cursor = session.wait_for(
            b"edit\r\n" + editor_frame(b"UNTITLED.TXT"), cursor
        )
        session.write(b"temporary\x18")
        cursor = session.wait_for(b"temporary\r\nnix> ", cursor)
        cursor = assert_files(session, (), cursor)
        halt(session, cursor)


def exercise_first_named_file(qemu: str, image: Path) -> bytes:
    filename = b"NOTES.TXT"
    full_document = b"A" * 510 + b"\r\n" + b"B" * 512 + b"C" * 512 + b"D" * 511
    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        session.write(b"echo tinx\by\r")
        cursor = session.wait_for(b"echo tinx\b \by\r\ntiny\r\nnix> ", cursor)
        session.write(b"nope\r")
        cursor = session.wait_for(
            b"nope\r\nUnknown command.\r\nnix> ", cursor
        )

        command = b"edit notes.txt"
        session.write(command + b"\r\n")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(filename), cursor
        )
        session.write(b"\x08\x7falpha\r\nbetx\x08y")
        cursor = session.wait_for(
            b"alpha\r\nbetx" + editor_frame(filename) + b"alpha\r\nbety",
            cursor,
        )
        session.write(b"\x0c")
        cursor = session.wait_for(editor_frame(filename), cursor)

        session.write(full_document + b"Z")
        cursor = session.wait_for(full_document + b"\x07", cursor)
        session.write(b"\x08D")
        cursor = session.wait_for(editor_frame(filename) + full_document, cursor)
        session.write(b"\x13")
        cursor = session.wait_for(
            editor_frame(filename, b"Saved.") + full_document, cursor
        )
        session.write(b"\x18")
        cursor = session.wait_for(b"\r\nnix> ", cursor)
        cursor = assert_files(session, (filename,), cursor)

        session.write(b"clear\r")
        cursor = session.wait_for(
            b"clear\r\n\x1b[2J\x1b[Hnix> ", cursor
        )
        session.write(b"reboot\r")
        cursor = session.wait_for(b"reboot\r\nRebooting...\r\n", cursor)
        cursor = session.wait_for(
            b"Nixodria OS\r\nType help.\r\nnix> ", cursor
        )
        cursor = assert_editor_document(
            session,
            filename,
            full_document,
            cursor,
            entered_filename=b"notes.txt",
            enter=b"\r\n",
        )

        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(filename) + full_document,
            cursor,
        )
        session.write(b"\x0ctemporary\x18")
        cursor = session.wait_for(
            editor_frame(filename) + b"temporary\r\nnix> ", cursor
        )
        cursor = assert_editor_document(session, filename, full_document, cursor)
        halt(session, cursor)
    return full_document


def exercise_second_named_file(
    qemu: str, image: Path, notes: bytes
) -> bytes:
    filename = b"COUNT.BAS"
    program = b'10 print "SECOND"\r\n20 end'
    successful_run = BASIC_FRAME + b"SECOND\r\n" + BASIC_FINISHED
    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        cursor = assert_files(session, (b"NOTES.TXT",), cursor)
        command = b"edit count.bas"
        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(filename), cursor
        )
        session.write(program + b"\x13")
        cursor = session.wait_for(
            program + editor_frame(filename, b"Saved.") + program, cursor
        )
        session.write(b"\x12")
        cursor = session.wait_for(successful_run, cursor)
        session.write(b" ")
        cursor = session.wait_for(editor_frame(filename) + program, cursor)
        session.write(b"\x18")
        cursor = session.wait_for(b"\r\nnix> ", cursor)

        cursor = assert_files(session, (b"NOTES.TXT", filename), cursor)
        cursor = assert_editor_document(
            session,
            filename,
            program,
            cursor,
            entered_filename=b"CoUnT.BaS",
        )
        cursor = assert_editor_document(session, b"NOTES.TXT", notes, cursor)

        session.write(b"reboot\r")
        cursor = session.wait_for(b"reboot\r\nRebooting...\r\n", cursor)
        cursor = session.wait_for(
            b"Nixodria OS\r\nType help.\r\nnix> ", cursor
        )
        cursor = assert_files(session, (b"NOTES.TXT", filename), cursor)
        cursor = assert_editor_document(session, b"NOTES.TXT", notes, cursor)
        cursor = assert_editor_document(session, filename, program, cursor)
        halt(session, cursor)
    return program


def exercise_process_restart(
    qemu: str, image: Path, basic_program: bytes
) -> bytes:
    replacement = b"saved across\r\nfull process restarts"
    filename = b"NOTES.TXT"
    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        command = b"edit NOTES.TXT"
        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(filename), cursor
        )
        session.write(b"\x0c")
        cursor = session.wait_for(editor_frame(filename), cursor)
        session.write(replacement + b"\x13")
        session.wait_for(
            replacement + editor_frame(filename, b"Saved.") + replacement,
            cursor,
        )
        # End QEMU as soon as the guest acknowledges the snapshot commit.
    assert_files_on_boot(
        qemu,
        image,
        ((filename, replacement), (b"COUNT.BAS", basic_program)),
    )
    return replacement


def exercise_readonly_failure(
    qemu: str,
    image: Path,
    expected: tuple[tuple[bytes, bytes], ...],
) -> None:
    before = image.read_bytes()
    filename, document = expected[0]
    with QemuSession(qemu, image, readonly=True) as session:
        cursor = session.wait_for(b"nix> ", 0)
        command = b"edit " + filename
        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(filename) + document, cursor
        )
        changed = document + b"!"
        session.write(b"!\x13")
        cursor = session.wait_for(
            b"!" + editor_frame(filename, b"Save failed.") + changed, cursor
        )
        session.write(b"\x18")
        cursor = session.wait_for(b"\r\nnix> ", cursor)
        halt(session, cursor)
    if image.read_bytes() != before:
        raise SmokeFailure("read-only save attempt changed the disk image")
    assert_files_on_boot(qemu, image, expected)


def exercise_injected_write_failures(
    qemu: str,
    source: Path,
    directory: Path,
    expected: tuple[tuple[bytes, bytes], ...],
) -> None:
    original = source.read_bytes()
    active_snapshot = slice(
        SNAPSHOT_OFFSETS[0], SNAPSHOT_OFFSETS[0] + SNAPSHOT_SIZE
    )
    target_header = SNAPSHOT_OFFSETS[1]
    target_payload = target_header + SECTOR_SIZE
    target_lba = SNAPSHOT_LBAS[1]
    configurations = {
        "invalidation": f"""
[inject-error]
event = "write_aio"
errno = "5"
sector = "{target_lba}"
""",
        "second-payload-sector": f"""
[inject-error]
event = "write_aio"
errno = "5"
sector = "{target_lba + 2}"
""",
        "final-header": f"""
[set-state]
event = "write_aio"
state = "1"
new_state = "2"

[inject-error]
event = "write_aio"
state = "2"
errno = "5"
sector = "{target_lba}"
""",
    }
    candidate = b"candidate blocked during snapshot save"
    candidate_files = ((expected[0][0], candidate),) + expected[1:]
    candidate_snapshot = build_snapshot(3, candidate_files)

    for name, configuration in configurations.items():
        image = directory / f"fault-{name}.img"
        config = directory / f"fault-{name}.conf"
        image.write_bytes(original)
        config.write_text(configuration.strip() + "\n", encoding="utf-8")

        with QemuSession(qemu, image, blkdebug_config=config) as session:
            cursor = session.wait_for(b"nix> ", 0)
            filename, document = expected[0]
            command = b"edit " + filename
            session.write(command + b"\r")
            cursor = session.wait_for(
                command + b"\r\n" + editor_frame(filename) + document,
                cursor,
            )
            session.write(b"\x0c")
            cursor = session.wait_for(editor_frame(filename), cursor)
            session.write(candidate + b"\x13")
            cursor = session.wait_for(
                candidate
                + editor_frame(filename, b"Save failed.")
                + candidate,
                cursor,
                timeout=10.0,
            )
            session.write(b"\x18")
            cursor = session.wait_for(b"\r\nnix> ", cursor)
            halt(session, cursor)

        failed = image.read_bytes()
        if failed[active_snapshot] != original[active_snapshot]:
            raise SmokeFailure(f"{name} failure changed the active snapshot")
        if name == "invalidation":
            if failed != original:
                raise SmokeFailure("failed invalidation changed the disk image")
        else:
            if any(failed[target_header : target_header + SECTOR_SIZE]):
                raise SmokeFailure(
                    f"{name} failure left a valid-looking target header"
                )
            payload = failed[target_payload : target_header + SNAPSHOT_SIZE]
            expected_payload = candidate_snapshot[SECTOR_SIZE:]
            if (
                name == "second-payload-sector"
                and payload[:SECTOR_SIZE] != expected_payload[:SECTOR_SIZE]
            ):
                raise SmokeFailure(
                    "payload failure did not commit its first sector"
                )
            if name == "final-header" and payload != expected_payload:
                raise SmokeFailure(
                    "final-header failure did not write the complete candidate"
                )
        assert_files_on_boot(qemu, image, expected)


def write_variant(
    qemu: str,
    directory: Path,
    name: str,
    data: bytearray,
    expected: tuple[tuple[bytes, bytes], ...],
) -> None:
    image = directory / f"recovery-{name}.img"
    image.write_bytes(data)
    assert_files_on_boot(qemu, image, expected)


def update_header_checksum(data: bytearray, slot: int) -> None:
    header = SNAPSHOT_OFFSETS[slot]
    data[
        header + HEADER_CHECKSUM_OFFSET : header + HEADER_CHECKSUM_OFFSET + 2
    ] = checksum16(
        data[header : header + HEADER_CHECKSUM_OFFSET]
    ).to_bytes(2, "little")


def save_after_recovery(
    qemu: str,
    image: Path,
    recovered: tuple[tuple[bytes, bytes], ...],
    replacement: bytes,
) -> tuple[tuple[bytes, bytes], ...]:
    filename, document = recovered[0]
    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        command = b"edit " + filename
        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(filename) + document, cursor
        )
        session.write(b"\x0c")
        cursor = session.wait_for(editor_frame(filename), cursor)
        session.write(replacement + b"\x13")
        cursor = session.wait_for(
            replacement + editor_frame(filename, b"Saved.") + replacement,
            cursor,
        )
        session.write(b"\x18")
        cursor = session.wait_for(b"\r\nnix> ", cursor)
        halt(session, cursor)
    result = ((filename, replacement),) + recovered[1:]
    assert_files_on_boot(qemu, image, result)
    return result


def exercise_recovery(
    qemu: str,
    source: Path,
    directory: Path,
    older: tuple[tuple[bytes, bytes], ...],
    newest: tuple[tuple[bytes, bytes], ...],
) -> None:
    original = source.read_bytes()
    newest_header = SNAPSHOT_OFFSETS[0]
    newest_payload = newest_header + SECTOR_SIZE
    variants: dict[str, bytearray] = {}

    bad_payload = bytearray(original)
    bad_payload[newest_payload] ^= 0x01
    variants["newest-payload"] = bad_payload

    bad_magic = bytearray(original)
    bad_magic[newest_header] ^= 0x01
    variants["newest-magic"] = bad_magic

    bad_file_crc = bytearray(original)
    bad_file_crc[
        newest_header + ENTRY_OFFSET + ENTRY_CHECKSUM_OFFSET
    ] ^= 0x01
    update_header_checksum(bad_file_crc, 0)
    variants["newest-file-crc"] = bad_file_crc

    bad_header_crc = bytearray(original)
    bad_header_crc[newest_header + HEADER_CHECKSUM_OFFSET] ^= 0x01
    variants["newest-header-crc"] = bad_header_crc

    bad_length = bytearray(original)
    length = newest_header + ENTRY_OFFSET + ENTRY_LENGTH_OFFSET
    bad_length[length : length + 2] = FILE_CAPACITY.to_bytes(2, "little")
    update_header_checksum(bad_length, 0)
    variants["newest-oversized-length"] = bad_length

    bad_count = bytearray(original)
    bad_count[newest_header + 6] = MAX_FILES + 1
    update_header_checksum(bad_count, 0)
    variants["newest-oversized-count"] = bad_count

    for name, data in variants.items():
        write_variant(qemu, directory, name, data, older)

    candidate_files = ((newest[0][0], b"candidate after interrupted save"),) + newest[1:]
    candidate = build_snapshot(3, candidate_files)
    candidate_header = candidate[:SECTOR_SIZE]
    candidate_payload = candidate[SECTOR_SIZE:]
    target_header = SNAPSHOT_OFFSETS[1]
    target_payload = target_header + SECTOR_SIZE

    invalidated = bytearray(original)
    invalidated[target_header : target_header + SECTOR_SIZE] = bytes(SECTOR_SIZE)
    write_variant(
        qemu, directory, "target-header-invalidated", invalidated, newest
    )

    for sectors in (2, 32):
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

    for prefix in (1, 7, HEADER_CHECKSUM_OFFSET, HEADER_CHECKSUM_OFFSET + 1):
        torn = bytearray(invalidated)
        torn[target_payload : target_header + SNAPSHOT_SIZE] = candidate_payload
        torn[target_header : target_header + prefix] = candidate_header[:prefix]
        write_variant(
            qemu, directory, f"target-header-prefix-{prefix}", torn, newest
        )

    completed = bytearray(original)
    install_snapshot(completed, 1, candidate)
    write_variant(
        qemu, directory, "completed-new-generation", completed, candidate_files
    )

    newest_b_corrupt = bytearray(completed)
    newest_b_corrupt[SNAPSHOT_OFFSETS[1] + SECTOR_SIZE] ^= 0x01
    fallback_image = directory / "recovery-newest-b-payload.img"
    fallback_image.write_bytes(newest_b_corrupt)
    assert_files_on_boot(qemu, fallback_image, newest)
    recovered_replacement = b"saved after fallback recovery"
    recovered = save_after_recovery(
        qemu, fallback_image, newest, recovered_replacement
    )
    parsed = newest_snapshot(fallback_image.read_bytes())
    if parsed != (1, 3, recovered):
        raise SmokeFailure(
            f"post-fallback save used wrong snapshot/generation: {parsed!r}"
        )

    both_payloads_bad = bytearray(original)
    both_payloads_bad[SNAPSHOT_OFFSETS[0] + SECTOR_SIZE] ^= 0x01
    both_payloads_bad[SNAPSHOT_OFFSETS[1] + SECTOR_SIZE] ^= 0x01
    write_variant(
        qemu, directory, "both-payloads-invalid", both_payloads_bad, ()
    )

    both_invalid = bytearray(original)
    for offset in SNAPSHOT_OFFSETS:
        both_invalid[offset : offset + SECTOR_SIZE] = bytes(SECTOR_SIZE)
    write_variant(qemu, directory, "both-snapshots-invalid", both_invalid, ())


def exercise_generation_order(
    qemu: str, template: bytes, directory: Path
) -> None:
    cases = (
        ("wrap-to-b", 0xFFFF, b"A old", 0, b"B new", b"B new"),
        ("wrap-to-a", 0, b"A new", 0xFFFF, b"B old", b"A new"),
        ("equal-prefers-a", 7, b"A tie", 7, b"B tie", b"A tie"),
        ("half-range-prefers-a", 0x8000, b"A half", 0, b"B half", b"A half"),
    )
    filename = b"WRAP.TXT"
    for name, generation_a, document_a, generation_b, document_b, expected in cases:
        image_data = bytearray(template)
        install_snapshot(
            image_data, 0, build_snapshot(generation_a, ((filename, document_a),))
        )
        install_snapshot(
            image_data, 1, build_snapshot(generation_b, ((filename, document_b),))
        )
        write_variant(qemu, directory, name, image_data, ((filename, expected),))

    writer_wrap = bytearray(template)
    before = ((filename, b"before writer wrap"),)
    install_snapshot(writer_wrap, 0, build_snapshot(0xFFFF, before))
    writer_image = directory / "generation-writer-wrap.img"
    writer_image.write_bytes(writer_wrap)
    after = save_after_recovery(qemu, writer_image, before, b"after writer wrap")
    wrapped = newest_snapshot(writer_image.read_bytes())
    if wrapped != (1, 0, after):
        raise SmokeFailure(f"writer generation did not wrap correctly: {wrapped!r}")


def exercise_legacy_recovery(
    qemu: str, template: bytes, directory: Path
) -> None:
    older = b"legacy older text"
    newest = b"legacy BASIC or text survives"
    image_data = bytearray(template)
    install_legacy_record(image_data, 0, build_legacy_record(4, older))
    install_legacy_record(image_data, 1, build_legacy_record(5, newest))
    image = directory / "legacy-nix2.img"
    image.write_bytes(image_data)
    expected = ((b"UNTITLED.TXT", newest),)
    assert_files_on_boot(qemu, image, expected)

    replacement = newest + b"!"
    converted = save_after_recovery(qemu, image, expected, replacement)
    assert_saved_snapshot(
        image,
        converted,
        template,
        expected_slot=0,
        expected_generation=6,
    )


def exercise_storage_full(
    qemu: str, template: bytes, directory: Path
) -> None:
    files = tuple(
        (f"FILE{index}.TXT".encode(), f"document {index}".encode())
        for index in range(1, MAX_FILES + 1)
    )
    image_data = bytearray(template)
    install_snapshot(image_data, 0, build_snapshot(7, files))
    image = directory / "storage-full.img"
    image.write_bytes(image_data)
    before = image.read_bytes()

    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        cursor = assert_files(
            session, tuple(filename for filename, _ in files), cursor
        )
        session.write(b"pkg install TETRIS.BAS\r")
        cursor = session.wait_for(
            b"pkg install TETRIS.BAS\r\nStorage full.\r\nnix> ", cursor
        )
        cursor = assert_files(
            session, tuple(filename for filename, _ in files), cursor
        )
        filename = b"NINTH.BAS"
        command = b"edit ninth.bas"
        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(filename), cursor
        )
        session.write(b'10 print "FULL"\x13')
        cursor = session.wait_for(
            b'10 print "FULL"'
            + editor_frame(filename, b"Storage full.")
            + b'10 print "FULL"',
            cursor,
        )
        session.write(b"\x18reboot\r")
        cursor = session.wait_for(
            b"\r\nnix> reboot\r\nRebooting...\r\n", cursor
        )
        cursor = session.wait_for(
            b"Nixodria OS\r\nType help.\r\nnix> ", cursor
        )
        cursor = assert_files(
            session, tuple(filename for filename, _ in files), cursor
        )
        halt(session, cursor)
    if image.read_bytes() != before:
        raise SmokeFailure("ninth-file save changed a full filesystem")


def save_empty_and_verify_restart(
    qemu: str,
    image: Path,
    expected: tuple[tuple[bytes, bytes], ...],
) -> tuple[tuple[bytes, bytes], ...]:
    filename, document = expected[0]
    with QemuSession(qemu, image) as session:
        cursor = session.wait_for(b"nix> ", 0)
        command = b"edit " + filename
        session.write(command + b"\r")
        cursor = session.wait_for(
            command + b"\r\n" + editor_frame(filename) + document, cursor
        )
        session.write(b"\x0c")
        cursor = session.wait_for(editor_frame(filename), cursor)
        session.write(b"\x13")
        cursor = session.wait_for(editor_frame(filename, b"Saved."), cursor)
        session.write(b"\x18")
        cursor = session.wait_for(b"\r\nnix> ", cursor)
        halt(session, cursor)
    result = ((filename, b""),) + expected[1:]
    assert_files_on_boot(qemu, image, result)
    return result


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
        if len(source_before) != IMAGE_SIZE:
            raise SmokeFailure(
                f"source image is {len(source_before)} bytes; expected {IMAGE_SIZE}"
            )
        packages = extract_packages(source_before)
        tetris_package = find_package(packages, b"TETRIS.BAS")
        find_package(packages, b"HELLO.BAS")
        with tempfile.TemporaryDirectory(prefix="nixodria-smoke-") as directory:
            root = Path(directory)

            basic_image = root / "basic.img"
            shutil.copyfile(source_image, basic_image)
            exercise_basic(qemu, basic_image, source_before)

            extended_basic_image = root / "extended-basic.img"
            shutil.copyfile(source_image, extended_basic_image)
            exercise_extended_basic(qemu, extended_basic_image)

            tetris_image = root / "tetris.img"
            shutil.copyfile(source_image, tetris_image)
            exercise_package_tetris(
                qemu, tetris_image, source_before, packages
            )

            package_lifecycle_image = root / "package-lifecycle.img"
            shutil.copyfile(source_image, package_lifecycle_image)
            exercise_package_lifecycle(
                qemu, package_lifecycle_image, source_before, packages
            )

            package_compaction_image = root / "package-compaction.img"
            shutil.copyfile(source_image, package_compaction_image)
            exercise_package_compaction(
                qemu, package_compaction_image, source_before, packages
            )

            exercise_corrupt_package_catalog(
                qemu, source_before, root, packages
            )
            exercise_package_write_failures(
                qemu, source_before, root, packages
            )

            corrupt_basic_image = root / "corrupt-basic.img"
            shutil.copyfile(source_image, corrupt_basic_image)
            exercise_corrupt_basic_module(
                qemu, corrupt_basic_image, tetris_package.source
            )

            filename_image = root / "filenames.img"
            shutil.copyfile(source_image, filename_image)
            exercise_filename_rules(qemu, filename_image)
            if filename_image.read_bytes() != source_before:
                raise SmokeFailure("unsaved filename checks changed their image")

            runtime_image = root / "nixodria.img"
            shutil.copyfile(source_image, runtime_image)
            notes = exercise_first_named_file(qemu, runtime_image)
            first_files = ((b"NOTES.TXT", notes),)
            first_save = assert_saved_snapshot(
                runtime_image,
                first_files,
                source_before,
                expected_slot=0,
                expected_generation=0,
            )
            if any(
                first_save[
                    SNAPSHOT_OFFSETS[1] : SNAPSHOT_OFFSETS[1] + SNAPSHOT_SIZE
                ]
            ):
                raise SmokeFailure("first save unexpectedly changed snapshot B")

            basic_program = exercise_second_named_file(qemu, runtime_image, notes)
            second_files = first_files + ((b"COUNT.BAS", basic_program),)
            second_save = assert_saved_snapshot(
                runtime_image,
                second_files,
                source_before,
                expected_slot=1,
                expected_generation=1,
            )
            snapshot_a = slice(
                SNAPSHOT_OFFSETS[0], SNAPSHOT_OFFSETS[0] + SNAPSHOT_SIZE
            )
            if second_save[snapshot_a] != first_save[snapshot_a]:
                raise SmokeFailure("second save changed the active fallback snapshot")

            replacement = exercise_process_restart(
                qemu, runtime_image, basic_program
            )
            newest_files = (
                (b"NOTES.TXT", replacement),
                (b"COUNT.BAS", basic_program),
            )
            third_save = assert_saved_snapshot(
                runtime_image,
                newest_files,
                source_before,
                expected_slot=0,
                expected_generation=2,
            )
            snapshot_b = slice(
                SNAPSHOT_OFFSETS[1], SNAPSHOT_OFFSETS[1] + SNAPSHOT_SIZE
            )
            if third_save[snapshot_b] != second_save[snapshot_b]:
                raise SmokeFailure("third save changed the active fallback snapshot")

            readonly_image = root / "readonly.img"
            shutil.copyfile(runtime_image, readonly_image)
            exercise_readonly_failure(qemu, readonly_image, newest_files)
            exercise_injected_write_failures(
                qemu, runtime_image, root, newest_files
            )
            exercise_recovery(
                qemu, runtime_image, root, second_files, newest_files
            )
            exercise_generation_order(qemu, source_before, root)
            exercise_legacy_recovery(qemu, source_before, root)
            exercise_storage_full(qemu, source_before, root)

            empty_files = save_empty_and_verify_restart(
                qemu, runtime_image, newest_files
            )
            fourth_save = assert_saved_snapshot(
                runtime_image,
                empty_files,
                source_before,
                expected_slot=1,
                expected_generation=3,
            )
            if fourth_save[snapshot_a] != third_save[snapshot_a]:
                raise SmokeFailure("fourth save changed the active fallback snapshot")

        if source_image.read_bytes() != source_before:
            raise SmokeFailure("smoke test modified the source build image")
    except (BrokenPipeError, OSError, SmokeFailure, ValueError) as error:
        print(f"smoke: {error}", file=sys.stderr)
        return 1

    print(
        "smoke: package install/remove, editable Tetris, named files, extended "
        "BASIC, persistence, recovery, corruption refusal, and write faults passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
