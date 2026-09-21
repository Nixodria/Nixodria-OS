# Nixodria OS

Nixodria OS 2 is a bootable Rust development environment. Its Rust shell runs on
the Linux kernel with Alpine Linux 3.24.2 userspace. The full `rustc` compiler,
Rust standard library, Cargo, and linker run **inside the booted OS**: write
Rust source, compile it, and run the resulting native executable from the
`nix>` terminal. QEMU provides the virtual machine; the host does not compile
your applications.

This changes the operating-system foundation. The former 16-bit BIOS kernel
and BASIC interpreter remain a separate legacy build. The new system uses the
Linux kernel; it does not claim a Rust kernel, a rustc port to the old kernel,
or a restricted Rust-like language. The Nixodria shell and bundled applications
are Rust source.

## Build and boot

The host needs Python 3.10 or newer, Make, and QEMU providing
`qemu-system-x86_64` and `qemu-img`. On macOS with Homebrew:

```sh
brew install python qemu
```

Build, check, and launch the system:

```sh
make
make check
make run
```

On a Mac whose selected Xcode installation blocks `make` with a license error,
an installed Command Line Tools toolchain can be used for this command:
`DEVELOPER_DIR=/Library/Developer/CommandLineTools make run`. The Python entry
point also works directly: `python3 tools/build_system.py build`, followed by
`python3 tools/build_system.py run`.

The first build needs an internet connection to download the pinned Alpine
virtual installation ISO and signed packages. It boots an installer VM, installs
the compiler and runtime onto a sparse 8 GiB virtual disk, then builds the
Nixodria shell with the **guest's** Rust toolchain. Host Rust, Cargo, NASM,
Docker, and root access are not needed. Allow time for package downloads and
compilation under emulation.

The default VM uses x86_64 TCG emulation and 2 GiB RAM, including on Apple
Silicon. The serial console runs at 115200 baud. `make run` starts the persistent
disk at `.nixodria/nixodria-rust.qcow2`; use `halt` for a clean shutdown. Control-C
interrupts a foreground guest program. Escape is handled by each application.

## Run in UTM on macOS

Install [UTM for macOS](https://docs.getutm.app/installation/macos/), then create
and open a ready-to-run VM:

```sh
make utm
open -a UTM .nixodria/Nixodria.utm
```

Press Play in UTM. The built-in terminal boots directly to `nix>`, where you can
edit, compile, and run Rust. Type `halt` to shut down cleanly. The bundle includes
its own writable disk, and saved files survive stopping and restarting UTM.
No installer ISO or manual virtual-hardware setup is needed.

The VM uses x86_64 emulation on both Apple Silicon and Intel Macs, BIOS boot,
2 GiB RAM, two virtual CPUs, VirtIO storage/networking, and a serial terminal.
This is a terminal OS; there is no graphical desktop. Apple Silicon runs this
x86_64 image through emulation, so compilation is slower than native ARM code.

The exporter copies the fresh system image and refuses to replace an existing
bundle. Your `make run` disk and your UTM disk are independent. `make clean`
preserves the default UTM bundle under `.nixodria/`. See the
[UTM guide](docs/utm.md) for export locations, existing-file migration, backups,
and manual settings.

## Write Rust inside Nixodria

At the `nix>` prompt:

```text
pkg install HELLO.rs
edit HELLO.rs
run HELLO.rs
```

`edit` opens the guest's `vi` editor; without a name it opens `untitled.rs`.
Press `i` to insert text, Escape to leave insert mode, and `:wq` followed by
Enter to save and exit. `:q!` discards unsaved
editor changes. The previous editor's Control-S/Control-R shortcuts do not
apply to `vi`.

For example, save this as `HELLO.rs`:

```rust
fn main() {
    println!("Hello from Rust inside Nixodria!");
}
```

`run HELLO.rs` invokes the guest compiler and executes the resulting program.
Compiler errors appear in the terminal. Rust's ownership checking, types,
generics, macros, and standard library come from the real compiler. Programs
can use files, collections, threads, processes, and networking through the
guest operating system.

Cargo projects work in the same terminal:

```text
cargo new demo
cd demo
cargo run
cargo test
```

A project using only the standard library can build offline. Other crates must
be downloaded first or supplied locally. Dependency build scripts execute
inside the guest too. The included C/C++ build tools support common native
dependencies, but a crate may require additional system packages.

## Commands and applications

| Command | Behavior |
| --- | --- |
| `help` | Show shell commands. |
| `files` | List files in the current directory. |
| `edit <file>` | Open a file in the guest editor. |
| `run <file.rs>` | Compile and run a Rust source file inside Nixodria. |
| `cd <directory>` | Change the current directory. |
| `cargo ...` / `rustc ...` | Run the installed Rust tools directly. |
| `pkg list` | List the bundled editable Rust applications. |
| `pkg install <file.rs>` | Copy application source into the current directory. |
| `pkg remove <file.rs>` | Remove that saved application source. |
| `printer <IP, URI, or queue>` | Configure printing through guest CUPS. |
| `print <file>` | Submit a saved file to the configured guest print queue. |
| `reboot` / `halt` | Restart or shut down the guest. |

Other executable commands run inside Nixodria. For shell operators, pipelines,
or redirection, use an explicit guest shell command such as
`sh -c 'printf "hello\n" > notes.txt'`.

The bundled catalog contains `HELLO.rs` and `TETRIS.rs`, copied from this
repository's `apps/` directory. Installation preserves an existing destination
instead of overwriting local edits. Installed applications are ordinary editable
files; removal deletes the selected source, so keep your changes before
removing it. The remote BASIC package catalog is used only by the legacy build.

```text
pkg install TETRIS.rs
run TETRIS.rs
```

Tetris contains its game rules in editable Rust source. Follow its displayed
controls. Its code compiles on the guest when you run it.

Nixodria applications must be published as editable Rust source. This policy
applies equally to the owner, maintainers, and other contributors. See
[CONTRIBUTING.md](CONTRIBUTING.md) for source-sharing and package-governance rules.

## Persistent files and updates

The console starts in `/root/workspace` inside the guest. Nixodria's own source
is installed at `/usr/src/nixodria`. Sources, Cargo
projects, binaries, and installed dependencies live on its disk and survive a
clean reboot. There is no eight-file or 2 KiB source limit. Available disk space
and RAM still limit what can be built.

`build/system/nixodria.qcow2` is the freshly built system image.
`.nixodria/nixodria-rust.qcow2` is your writable runtime image. Rebuilding the
base image does not replace an existing runtime disk. To install the current
Nixodria source into that disk and rebuild its shell inside the guest:

```sh
make update
```

Shut down an interactive VM before updating its disk. The update preserves
workspace files and installs any newly required system packages. An update
that adds packages needs internet access; unchanged package sets work offline.
`make clean` removes build output and retains `.nixodria/`.
Updates replace the managed source under `/usr/src/nixodria` and refresh the
bundled catalog, including removing retired entries. Keep personal source and
edits under `/root/workspace`; installed application copies there are preserved.
Back up the runtime disk while the VM is shut down; it may contain private
source code, credentials, or other files. Do not publish it as a blank image.

Existing BASIC images at `.nixodria/nixodria.img` remain separate. Export their
saved files without modifying the image:

```sh
make export-legacy OUTPUT=./legacy-files
```

To copy verified legacy files directly into the persistent Rust guest:

```sh
make import-legacy
```

Import creates a directory named `/root/workspace/legacy-<image-hash12>` inside
the guest, preserves filenames, and refuses to overwrite existing files. Shut
down the interactive guest before importing. Neither operation modifies the
legacy floppy. Export writes to the host; import writes to the new guest disk.
Both preserve the original text and do not translate BASIC into Rust.
See [the legacy guide](docs/legacy.md) to run the old system and inspect its
language, editor, and storage format.

## Printing and network access

Printing uses CUPS and its client tools **inside Nixodria**. The guest connects
to the configured printer through QEMU's network connection; no host print
dialog, host CUPS installation, or host `lp` command performs the job. Use
`printer` and `print` to select a destination and submit saved files. Printer
compatibility depends on its protocol and supported formats. A queued job is
not evidence that a physical page finished printing; physical output needs a
separate check.

The VM uses QEMU user-mode networking without host port forwarding. The local
serial console opens the shell as root for personal development. Applications,
Cargo build scripts, and commands therefore have full guest privileges. This
is not an application sandbox or a configured multi-user server.

## Validation and build provenance

```sh
make check
make smoke
```

`make check` runs the host checks and validates the qcow2 image. `make smoke`
boots a disposable copy without network access and exercises native Rust
compilation, standard-library behavior, Cargo, source changes, and persistence
across restart. These checks are separate from testing arbitrary third-party
crates, real hardware, or physical printer output.

`system.lock.json` pins the Alpine 3.24.2 ISO SHA-256 and the Rust/Cargo
`1.96.1-r0` packages. Alpine verifies package signatures during installation.
Transitive system packages come from the maintained Alpine `v3.24` repositories
and can change. The installed package versions are recorded in
`/usr/lib/nixodria/packages.txt`, and compiler versions in
`/usr/lib/nixodria/toolchain.txt`. This build is not claimed to produce identical
disk-image bytes across builds.

## Run from Nixodria for Android

The [Nixodria Android app](https://github.com/Nixodria/Nixodria) provides an
Alpine userspace. Install the x86_64 system emulator and build tools there, then
use this repository's normal workflow:

```sh
apk add --no-cache git make python3 qemu-system-x86_64 qemu-img
git clone https://github.com/Nixodria/Nixodria-OS.git
cd Nixodria-OS
make
make run
```

This boots the same Nixodria OS disk in a full virtual machine. Keep the
checkout and runtime disk in storage that survives the Android app's reset
workflow. The VM still needs its configured 2 GiB RAM and sufficient disk space;
this Android route requires separate device testing.
