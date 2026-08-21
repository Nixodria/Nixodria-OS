# Nixodria OS

Nixodria OS is a bootable x86 command-line operating system. Its first
512-byte BIOS sector loads a seven-sector real-mode kernel. Ten more sectors
hold two alternating copies of the text editor's durable document, producing a
9 KiB, 18-sector image. The kernel starts a serial console without a general
filesystem, processes, or networking.

## Commands

- `help` — list commands
- `edit` — open the persistent text editor
- `clear` — clear the terminal
- `echo <text>` — print text
- `reboot` — restart the OS through the BIOS
- `halt` — stop the CPU

The shell input line holds 31 characters and supports Backspace.

## Text editor

Run `edit` to open a 2 KiB scratchpad. Type at the end of the document, use
Enter for a new line, and use Backspace to remove text. Both the BS and DEL
terminal codes are accepted as Backspace. The editor redraws after deletion so
editing remains correct across terminal line wraps.

- Control-X — exit to the shell
- Control-S — save the document to disk
- Control-R — run the current document as a BASIC program
- Control-L — clear the entire document

The editor holds one document of up to 2,047 bytes. Control-S writes the current
document to Nixodria's reserved storage sectors; the last valid save is loaded
automatically after a reboot or a later QEMU launch. Control-X does not save, so
unsaved edits are discarded at the next boot. Saves alternate between two
records with generation numbers plus header and document CRC-16 checksums. If a
save is interrupted or its newest record is corrupt, the editor recovers the
previous verified version. A failed disk write is reported instead of being
presented as a successful save. The inactive record intentionally retains that
one previous version for recovery.

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

The editor stores one program of at most 2,047 bytes. This BASIC subset does not
provide multiple files, interactive input, string variables, `FOR`/`NEXT`,
arrays, functions, multiplication, division, parentheses, or multiple
statements on one line.

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
its boot and kernel sectors, while preserving its saved document; `make clean`
also leaves this runtime copy intact.

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
