# Nixodria OS

Nixodria OS is a tiny bootable x86 command-line operating system. Its first
512-byte BIOS sector loads a two-sector real-mode kernel, producing a 1.5 KiB
image. The kernel starts a serial console without a filesystem, processes, or
networking.

## Commands

- `help` — list commands
- `edit` — open the in-memory text editor
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
- Control-L — clear the entire document

The editor retains up to 2,047 bytes while the OS remains booted. Its contents
are stored only in RAM and are cleared by `reboot`; Nixodria does not yet have a
filesystem.

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

The generated image is `build/nixodria.img`.
