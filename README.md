# Nixodria OS

Nixodria OS is a bootable x86 command-line operating system. Its first
512-byte BIOS sector loads a ten-sector real-mode kernel from a standard
1.44 MiB floppy image. A small flat file store holds up to eight named text or
BASIC files in two alternating recovery snapshots. The kernel starts a serial
console without processes or a general-purpose filesystem. A demand-loaded
module provides the BASIC interpreter, while another supplies the small network
stack and rasterizer used for direct IPP printing. The image also bundles an
integrity-checked catalog of editable BASIC packages published from the
[Nixodria Packages](https://github.com/Nixodria/Nixodria-Packages) repository.

## Commands

- `help` — list commands
- `files` — list saved files by name
- `edit <filename>` — open an existing file or start a new one
- `edit` — open `UNTITLED.TXT` for compatibility
- `run <filename>` — run a saved BASIC source file
- `pkg list` — list packages available in the embedded catalog
- `pkg install <filename>` — install a package as an editable saved file
- `pkg remove <filename>` — remove a saved file installed under that name
- `print <filename>` — rasterize and queue a saved file on the configured printer
- `printer` — show the configured printer IPv4 address
- `printer <IPv4>` — set the printer address for the current boot
- `clear` — clear the terminal
- `echo <text>` — print text
- `reboot` — restart the OS through the BIOS
- `halt` — stop the CPU

The shell input line holds 31 characters and supports Backspace. While a BASIC
program or installed package is running, Escape stops it and returns directly
to the `nix>` terminal. Escape is reserved by the runtime and is not delivered
to the program through `KEY`.

## Text editor

Run `edit NOTES.TXT` or `edit GAME.BAS` to open a 2 KiB scratchpad under that
filename. Names are case-insensitive, may contain up to 12 letters, digits,
dots, underscores, or hyphens, and are displayed in uppercase. `.TXT` and
`.BAS` are conventions; both use the same text editor. Type at the end of the
document, use Enter for a new line, and use Backspace to remove text. Both the
BS and DEL terminal codes are accepted as Backspace. The editor redraws after
deletion so editing remains correct across terminal line wraps.

- Control-X — exit to the shell
- Control-S — save the document to disk
- Control-R — run the current document as a BASIC program
- Control-L — clear the entire document

Each of the eight file slots holds up to 2,047 bytes. Control-S saves only the
open file; `files` then lists its filename so it can be reopened with
`edit <filename>`. Control-X does not save, so edits made since the last
Control-S are discarded after a reboot or later QEMU launch. Saves alternate
between two complete directory snapshots with generation numbers plus header
and per-file CRC-16 checksums. If a save is interrupted or the newest snapshot
is corrupt, the editor recovers the previous verified snapshot. Failed disk
writes and a full eight-file directory are reported instead of being presented
as successful saves.

Control-R runs the document currently in memory, including unsaved edits; it
does not save implicitly. Program output is shown on the serial console. A
syntax or runtime error stops the program; an ordinary dismissal key returns
safely to the editor, while Escape returns to the shell. Neither path changes
the source.

## Packages

The package catalog is a pinned, reproducible snapshot of
[`Nixodria/Nixodria-Packages`](https://github.com/Nixodria/Nixodria-Packages).
Every package is complete Nixodria BASIC source; the catalog contains no opaque
application binaries. List and install the initial packages from the shell:

```text
pkg list
pkg install HELLO.BAS
run HELLO.BAS
```

An installed package occupies one of the same eight durable file slots used by
the editor. It appears under `files`, remains editable with `edit <filename>`,
and persists across reboot. Installation refuses to overwrite a saved file of
the same name, protecting local edits. To replace it with the catalog copy,
explicitly run `pkg remove <filename>` and then install it again. Removal
removes that saved source from the active directory after committing a new
recoverable snapshot. Because installed source remains an ordinary editable
file, removal works by saved filename even if that package is no longer in the
current catalog; use it carefully, because the OS does not retain separate
package-provenance metadata.

The package catalog is immutable while the OS is running. Its headers, source
lengths, filenames, and source payloads are protected by CRC-16 checks, and the
host build verifies the entire pinned release artifact with SHA-256 before
placing it in the floppy image. Nixodria's real-mode network stack does not
claim to download from GitHub directly: it has no DNS or TLS implementation.

The Nixodria project owner retains the right to define, change, and enforce the
rules for packages distributed through the official OS package manager. Those
rules may cover eligibility, source and format requirements, compatibility,
safety, quality, review, versioning, or removal. Official changes are published
in the Nixodria OS and Packages repositories so the active rules remain visible
to users and contributors. The examples above do not limit that authority; the
owner may establish other package rules as the project evolves. Until a
published rule changes, it applies to every official package submission and
maintainer action, including those made by the project owner.

## Tetris

Install and start the Tetris package:

```text
pkg install TETRIS.BAS
run TETRIS.BAS
```

The 10-by-20 board uses `0` for an empty cell and `1` for a filled cell. The
status below it shows the score (`S`) and cleared-line count (`L`). Controls are:

- `a` / `d` — move left / right
- `w` — rotate clockwise
- `s` — soft drop
- Space — hard drop
- `q` — end the game; press any key at the BASIC completion prompt to return
- `r` — restart after game over
- Escape — stop the package and return to the `nix>` terminal

All tetromino rules, collision checks, line compaction, scoring, and game state
live in the editable
[`packages/TETRIS.BAS`](https://github.com/Nixodria/Nixodria-Packages/blob/v1.0.1/packages/TETRIS.BAS)
source, not native assembly. Run `edit TETRIS.BAS` after installation to inspect
or change it.

## BASIC

Nixodria implements a deliberately small, case-insensitive, line-numbered BASIC
subset. Every physical line starts with a decimal line number from 0 through
65535. Supported statements are:

- `PRINT "text"` or `PRINT expression`; a trailing `;` suppresses the newline
- `LET A = expression`, using one-letter variables `A` through `Z`; `LET` is
  optional
- `DIM A(max)` plus indexed `A(expression)` reads and assignments for one array
- `IF expression = expression THEN line`, with `<` and `>` also supported
- `GOTO line`
- `GOSUB line` and `RETURN`, with at most 16 nested calls
- `CLS` to clear the serial terminal
- `KEY A` to store one pending input byte in `A`, or zero when none is ready;
  Escape is reserved for returning to the OS terminal
- `WAIT expression` to pause for a positive number of BIOS timer ticks
- `TIMER A` to store the low 16 bits of the BIOS tick counter in `A`
- `REM` followed by a comment
- `END`

Expressions use signed 16-bit integers, scalars, array elements, and
parentheses. Multiplication, division, and `MOD` bind before `+` and `-`; the
bitwise `AND` operator binds after arithmetic. Division truncates toward zero,
and division by zero is a runtime error. Scalars and the declared array start
at zero on every run. An array's inclusive maximum index can be 0 through 255.

Physical lines execute in source order and may contain multiple statements
separated by colons. `GOTO`, `GOSUB`, and `THEN` use the first matching line
number. A program may execute 10,000 statements without yielding; each
successful `WAIT` starts a fresh allowance so interactive programs can keep
running while an accidental tight loop still returns a runtime error. One BIOS
tick is approximately 55 ms. `KEY` reads raw COM1 bytes without echo, and
`TIMER` values can appear negative because BASIC integers are signed words.

For example:

```basic
10 REM COUNT DOWN
20 LET A = 3
30 PRINT "COUNT"
40 PRINT A
50 LET A = A - 1
60 IF A > 0 THEN 40
70 END
```

Each BASIC file can contain a program of at most 2,047 bytes. The flat file
store does not provide rename or a separate general-purpose delete command;
`pkg remove <filename>` explicitly deletes the matching saved file.
This BASIC subset does not provide string variables, `FOR`/`NEXT`, functions,
or more than one array.

## Native printing

`print NOTES.TXT` reads the saved file, lays it out with the BIOS bitmap font,
and submits 300 dpi grayscale Letter pages directly to
`ipp://<printer>:631/ipp/print`. The OS contains its own polled NE2000 driver,
ARP/IPv4/TCP client, HTTP/IPP encoder, text renderer, and PWG Raster encoder. It
does not invoke a macOS print dialog, printer driver, CUPS queue, `lp`, or
host-side renderer. QEMU's virtual Ethernet adapter and user-mode NAT only
carry the guest's packets to the LAN.

The default address is `192.168.40.220`, the Brother MFC-J6555DW configured for
this installation. If DHCP changes it, run `printer 192.168.x.x` before
printing. The setting lasts until reboot. Another printer can be used when it
offers unauthenticated IPP on port 631 at `/ipp/print` and accepts
`image/pwg-raster` with 300 dpi `sgray_8` pages.

Only files already saved with Control-S can be printed. Each page holds 93
columns by 96 lines; long lines wrap, and an empty file produces one blank
page. `Print job queued.` means the printer returned a successful IPP status;
it does not claim that the sheet has physically finished. A timeout or lost
response is reported as a failure and the OS does not open a new connection to
replay an ambiguous job. The transport is unencrypted local-network IPP, so
use it only on a trusted LAN.

## Build and run

NASM, Python 3, GNU Make, and QEMU are required. On macOS with Homebrew:

```sh
brew install nasm qemu
```

Build the bootable image. The first build downloads the catalog pinned in
`packages.lock.json`, verifies its size and SHA-256 digest, and caches it under
`.nixodria/`:

```sh
make
```

For an offline or package-development build, provide an already verified or
locally built catalog explicitly:

```sh
make PACKAGE_CATALOG=/path/to/nixodria-packages.bin
```

The override still receives structural and CRC validation, but it bypasses the
release SHA-256 pin; use it only with a catalog you trust.

Check its exact image layout and BIOS signature, then boot it in QEMU and
exercise the command loop and editor:

```sh
make check
make smoke
```

Start an interactive session:

```sh
make run
```

Press Control-C to stop QEMU. The console uses COM1 at 38400 baud, 8 data bits,
no parity, and one stop bit. The run target also creates a fixed NE2000 ISA
adapter at I/O `0x300` with QEMU user-mode networking so the guest can reach a
printer on the host's LAN.

The reproducible blank-snapshot image is `build/nixodria.img`. `make run` boots
the gitignored `.nixodria/nixodria.img` runtime copy. Normal rebuilds refresh
only its boot, kernel, BASIC and print modules, package catalog, and unused
sectors while preserving every saved file; `make clean` leaves both the runtime
copy and verified package-catalog cache intact. When an older
single-document runtime image is first refreshed, its last valid save
immediately appears as `UNTITLED.TXT`; the next save converts the disk record to
the named-file format. Existing named files, including a saved or edited
`TETRIS.BAS`, survive the refresh unchanged. The former immutable bundled
Tetris source is now supplied by the catalog, so users who never saved a local
copy install it once with `pkg install TETRIS.BAS` after booting the refreshed
image.

### Run inside Nixodria for Android

[Nixodria](https://github.com/Nixodria/Nixodria) provides an Alpine userland,
not a BIOS virtual machine, so this x86 image runs there through Alpine's
full-system QEMU package. In a Nixodria terminal:

```sh
apk add --no-cache git make nasm python3 qemu-system-i386
git clone https://github.com/Nixodria/Nixodria-OS.git
cd Nixodria-OS
make check
make run
```

Keep the checkout under `/root` if editor saves should survive Nixodria's
normal Linux reset. The run target uses TCG software emulation, needs no KVM or
root access, connects COM1 to Nixodria's terminal, and creates an emulated
NE2000 adapter for native printing. Nixodria also provides a first-class
dashboard action that performs a pinned, verified version of this setup after
confirmation.
