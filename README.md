# Nixodria OS

Nixodria OS is a bootable x86 command-line operating system. Its first
512-byte BIOS sector loads a ten-sector real-mode kernel from a standard
1.44 MiB floppy image. A small flat file store holds up to eight named text or
BASIC files in two alternating recovery snapshots. The kernel starts a serial
console without processes, networking, or a general-purpose filesystem.

## Commands

- `help` — list commands
- `files` — list saved files by name
- `edit <filename>` — open an existing file or start a new one
- `edit` — open `UNTITLED.TXT` for compatibility
- `clear` — clear the terminal
- `echo <text>` — print text
- `reboot` — restart the OS through the BIOS
- `halt` — stop the CPU

The shell input line holds 31 characters and supports Backspace.

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
does not save implicitly. Program output is shown on the serial console, and a
syntax or runtime error stops the program and returns safely to the editor
without changing its source.

## BASIC

Nixodria implements a deliberately small, case-insensitive, line-numbered BASIC
subset. Every statement starts with a decimal line number from 0 through 65535.
Supported statements are:

- `PRINT "text"` or `PRINT expression`
- `LET A = expression`, using one-letter variables `A` through `Z`
- `IF expression = expression THEN line`, with `<` and `>` also supported
- `GOTO line`
- `REM` followed by a comment
- `END`

Expressions contain signed 16-bit integer literals or variables joined with
`+` and `-`. Variables start at zero and are reset each time Control-R starts a
program. Lines execute in the order written, and `GOTO` or `THEN` uses the
first matching line number. Execution stops with a runtime error after 10,000
statements, so an accidental infinite loop returns control to the editor.

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
store does not provide rename or delete operations. This BASIC subset does not
provide interactive input, string variables, `FOR`/`NEXT`, arrays, functions,
multiplication, division, parentheses, or multiple statements on one line.

## Build and run

NASM, Python 3, GNU Make, and QEMU are required. On macOS with Homebrew:

```sh
brew install nasm qemu
```

Build the bootable image:

```sh
make
```

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
no parity, and one stop bit.

The reproducible blank image is `build/nixodria.img`. `make run` boots the
gitignored `.nixodria/nixodria.img` runtime copy. Normal rebuilds refresh only
its boot and kernel sectors while preserving every saved file; `make clean`
also leaves this runtime copy intact. When an older single-document runtime
image is first refreshed, its last valid save immediately appears as
`UNTITLED.TXT`; the next save converts the disk record to the named-file format.

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
root access, connects COM1 to Nixodria's terminal, and creates no virtual
network device. Nixodria also provides a first-class dashboard action that
performs a pinned, verified version of this setup after confirmation.
