# Nixodria OS

Nixodria OS is a bootable command-line operating system contained in one
512-byte x86 BIOS boot sector. It starts a serial console directly, without a
bootloader, filesystem, processes, or networking.

## Commands

- `help` — list commands
- `clear` — clear the terminal
- `echo <text>` — print text
- `reboot` — restart the OS through the BIOS
- `halt` — stop the CPU

The input line holds 31 characters and supports Backspace.

## Build and run

NASM, Python 3, GNU Make, and QEMU are required. On macOS with Homebrew:

```sh
brew install nasm qemu
```

Build the bootable image:

```sh
make
```

Check its exact size and BIOS signature, then boot it in QEMU and exercise the
command loop:

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
