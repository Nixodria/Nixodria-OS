# Nixodria OS

Nixodria OS is a bootable x86 command-line operating system. Its first
512-byte BIOS sector loads a three-sector real-mode kernel. Ten more sectors
hold two alternating copies of the text editor's durable document, producing a
7 KiB image. The kernel starts a serial console without a general filesystem,
processes, or networking.

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
