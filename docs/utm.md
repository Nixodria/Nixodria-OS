# Nixodria in UTM on macOS

Nixodria OS 2 uses UTM's QEMU backend, configured for Apple Silicon and Intel
Macs. The current image is x86_64 and uses BIOS boot. Select **Emulate**
when creating a VM manually. The generated bundle already contains the required
settings, including a built-in serial terminal instead of a graphical display.

## Create and open the VM

Install [UTM](https://docs.getutm.app/installation/macos/) and the normal host
build dependencies described in the [README](../README.md). From the checkout:

```sh
make utm
open -a UTM .nixodria/Nixodria.utm
```

If `make` is unavailable because of an Xcode license prompt, use the Python
entry points:

```sh
python3 tools/build_system.py build
python3 tools/export_utm.py
open -a UTM .nixodria/Nixodria.utm
```

Press Play in UTM and wait for `nix>`. Try:

```text
pkg install HELLO.rs
run HELLO.rs
cargo new demo
cd demo
cargo run --offline
cd ..
```

`edit HELLO.rs` opens `vi`: press `i` to insert, Escape to leave insert mode, and
`:wq` then Enter to save. Control-C interrupts a foreground command. Use `halt`
at the Nixodria prompt for a clean shutdown before backing up or moving the VM.

The exporter creates `.nixodria/Nixodria.utm/config.plist` and a standalone
`Data/nixodria.qcow2`. UTM may add its own metadata or copy the bundle during
import. Use UTM's Show in Finder action to locate the active bundle before
backing it up. Keep the whole bundle together. Its disk has 8 GiB virtual
capacity and consumes host space as files are written.

## Preserve saved work

Exports always refuse an existing destination, including symlinks. Rebuilding
the base image does not update or replace a UTM disk. The default bundle is
outside `build/`, so `make clean` retains it. To create another fresh VM, choose
a different path:

```sh
make utm UTM_OUTPUT="$HOME/Documents/Nixodria-fresh.utm"
```

By default, UTM starts with the clean system image, not the contents of the
separate `make run` disk. To carry your existing workspace into a new UTM VM,
first shut down the command-line guest using `halt`, then explicitly export its
disk:

```sh
python3 tools/export_utm.py \
  --image .nixodria/nixodria-rust.qcow2 \
  --output "$HOME/Documents/Nixodria-with-work.utm"
```

The exporter copies the source without modifying it and checks the resulting
disk. It refuses a source locked by a running VM. After exporting, the disks
are independent; changes in one do not appear in the other. `make update`
updates only the command-line runtime disk, not your UTM VM. Back up personal
files before replacing an OS installation, and never distribute a used VM as
a clean image: it may contain private files and credentials.

## Manual settings and troubleshooting

If importing a disk manually, use these settings:

| Setting | Value |
| --- | --- |
| Backend | QEMU / Emulate |
| Architecture | x86_64 |
| System | Standard PC (i440FX + PIIX, `pc`) |
| CPU | Default, 2 cores |
| Memory | 2048 MiB |
| UEFI boot | Off (BIOS/SeaBIOS) |
| Hypervisor | Off |
| Disk | Writable qcow2, VirtIO interface |
| Display / sound | None |
| Serial | First serial port, built-in Terminal, automatic target |
| Network | Emulated VLAN (user networking), VirtIO NIC |

A UEFI shell usually means UEFI was enabled for this BIOS disk. A blank VGA
window usually means the serial terminal is missing: Nixodria's login shell
runs on `ttyS0` at 115200 baud. No host port forwards, shared folders, or guest
agent are required. Networking is outbound user networking; the local guest
console runs as root, as in `make run`.

The packaged settings deliberately use emulation on both Mac architectures.
Large Rust builds can be slow on Apple Silicon because the guest compiler is
x86_64. Increasing RAM may help larger projects, but does not change the guest
architecture.

## Verified configuration

Verified on 2026-09-21 with UTM 4.7.5 on Apple Silicon running macOS 26.6.2:

- Imported the generated bundle and booted directly into the built-in terminal.
- Ran `sh /usr/lib/nixodria/selftest.sh` successfully inside UTM, including Rust
  standard-library behavior, Cargo, shell tests, Tetris tests, and CUPS filtering.
- Created and saved Rust source with `vi`, compiled it with `run`, shut down
  using `halt`, restarted the VM, and compiled the same saved source again.
- Verified Control-C interrupts a foreground command, DHCP configures the
  VirtIO NIC, and DNS/HTTPS can reach the official Alpine download server.
- Checked the exported qcow2 after shutdown with `qemu-img check`.

`make check` passed all 43 host tests, including export preservation, conversion
failure cleanup, publication races, and backing-file flattening. The existing
`tests/smoke_rust.py` also passed separately using command-line QEMU. Intel Mac
execution has not been tested on physical Intel hardware.
