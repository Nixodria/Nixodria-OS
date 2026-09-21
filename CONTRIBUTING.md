# Contributing to Nixodria OS

Nixodria OS 2 provides Rust development with compilation and execution inside
the booted OS. Its foundation is the Linux kernel and Alpine userspace, with a
Rust shell and editable Rust applications. The former BIOS/BASIC system remains
a separate legacy target.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
Search the [issue tracker](https://github.com/Nixodria/Nixodria-OS/issues) for
related work, keep changes focused, and explain compatibility consequences.
An existing issue is helpful context, not a prerequisite for a clear fix.

## Application policy: editable Rust source

Every application contributed for inclusion in the current Nixodria OS must be
written and shared as editable Rust source. This includes games, calculators,
utilities, and other Nixodria applications. The bundled catalog currently lives
in this repository's `apps/` directory. The external Nixodria Packages BASIC
catalog remains part of the legacy build and is not the current Rust catalog.

This requirement applies equally to the founder, maintainers, organization
members, and first-time contributors. A role or repository permission does not
create an exception. Application behavior must remain in its published source;
do not hide game or utility logic in the shell or ship only an opaque binary.

Use the real guest Rust compiler and standard library. Applications must not
require an unpublished language variant, private runtime changes, or a host
compiler to work in Nixodria. Publish every required Nixodria source or runtime
change in the same contribution or a linked prerequisite, with documentation
and relevant verification. Coordinate dependent releases so that the required
runtime is available before an application depends on it.

The application-language policy does not require rewriting existing upstream
system dependencies. Linux, Alpine, the C library, compiler backends, `vi`, CUPS,
and other platform utilities may use their upstream implementation languages.
Build and test tooling may use Python or shell. These dependencies support Rust
development; they are not a route for submitting a new Nixodria application in
another language. The retained BIOS/BASIC code is historical compatibility code.

### Package governance

The Nixodria project owner retains the right to create, revise, replace, and
enforce rules for packages distributed through the official package manager.
This includes eligibility, source formats, compatibility, safety and quality,
review, versioning, installation, deprecation, and removal. These examples do
not limit that authority.

Rule changes become official when published in the Nixodria OS or Nixodria
Packages repository and reflected in the implementation when necessary. Until a
published rule changes, it applies to every official contribution and maintainer
action, including those of the owner. Forks may use different rules but must not
present their catalogs as the official Nixodria catalog. Past inclusion does not
guarantee continued distribution.

## Development environment

The default build needs Python 3.10 or newer, Make, `qemu-system-x86_64`, and
`qemu-img` on the host. The initial installation downloads the pinned Alpine
ISO and signed packages. Rust and Cargo are installed and executed in the guest;
host Rust, NASM, Docker, and root access are not required.

```sh
brew install python qemu
make
make check
make smoke
```

The VM defaults to 2 GiB RAM, an 8 GiB sparse disk, and x86_64 TCG emulation.
Building large projects may need more resources. Run `make run` to use the
persistent guest, and shut it down before running `make update` to rebuild the
shell in that guest from your checkout.

## Repository layout

- `Cargo.toml` and `src/main.rs` define the Rust shell.
- `apps/HELLO.rs` and `apps/TETRIS.rs` are editable bundled applications.
- `guest/install.sh` builds and installs Nixodria inside the guest.
- `guest/selftest.sh` exercises the installed guest compiler and Cargo.
- `system.lock.json` pins the installer ISO and Rust/Cargo package versions.
- `tools/build_system.py` prepares and verifies the new system image.
- `tools/export_legacy_files.py` and `tools/import_legacy_files.py` recover
  verified old saves to the host or the persistent Rust guest.
- `build/system/nixodria.qcow2` is the fresh system image.
- `.nixodria/nixodria-rust.qcow2` is the persistent writable runtime image.
- `tools/legacy.mk`, `src/*.asm`, `packages.lock.json`, and the legacy image
  tools/tests preserve the BIOS/BASIC implementation.
- [docs/legacy.md](docs/legacy.md) documents the old system and file export.

Generated images, downloaded artifacts, caches, and runtime state must not be
committed. A runtime image can contain private source and credentials.

## Changing the system

### Keep compilation in the guest

Preserve the complete workflow: edit source in Nixodria, invoke its `rustc` or
Cargo, and execute the resulting program there. A host compilation producing a
guest binary does not verify this workflow. Use standard Rust behavior and
report unsupported dependencies honestly.

Keep the shell's source catalog editable. Installing an entry must not silently
overwrite local changes. Make it clear when an explicit `sh -c` invocation is
needed for expansion or redirection.

### Protect persistent data

Rebuilding the base image must not replace an existing runtime disk. Updates
must preserve workspace files and report failures accurately. Avoid concurrent
VM access to a writable image. Refuse ambiguous paths or incompatible image
formats rather than guessing which disk should be modified.

Legacy `.nixodria/nixodria.img` files must remain separate from the new qcow2
disk. Export reads verified saved text into a host directory. Import copies
that text into a separate `/root/workspace/legacy-<image-hash12>` guest directory
without overwriting existing files. Neither modifies the original image or
translates BASIC into Rust. Preserve legacy snapshot validation and recovery
when changing those tools. `make clean` must retain runtime files and caches.

### Keep provenance clear

Verify the installation ISO against `system.lock.json`, keep Alpine package
signature verification enabled, and record installed package versions.
Rust/Cargo are pinned; their transitive packages come from the maintained Alpine
branch. Do not describe the entire disk image as reproducible unless its inputs
and image-generation behavior have actually been made reproducible.

The local serial shell runs as root. Programs and Cargo build scripts have full
guest privileges; do not imply application isolation. QEMU uses user networking
without incoming port forwarding. Adding a network service, privilege boundary,
or host integration requires corresponding documentation and verification.

Keep commands, editor controls, package behavior, and migration instructions in
the README synchronized with the implementation. State when physical printer,
Android device, or hardware checks have not been run.

## Verification

- `make` bootstraps the disk and compiles the shell inside the guest.
- `make check` runs host checks and qcow2 validation.
- `make smoke` boots a disposable image without network access and checks native
  Rust/standard-library behavior, Cargo, source changes, and restart persistence.
- `make run` opens the persistent interactive guest.
- `make legacy` and `make legacy-smoke` build and exercise the retained BIOS
  system; these need NASM and `qemu-system-i386` as described in the legacy guide.

For behavioral changes, extend the closest relevant check with observable
behavior. Compiler success alone is not enough for terminal handling,
persistence, or printing. Tests that run in a disposable VM must not overwrite
the user's runtime disk. Separate mocked printer transport tests from physical
output.

Run applicable checks and `git diff --check` before submitting. For a
documentation-only change, verify commands and links and run the whitespace
check. Report exact results and skipped checks in the pull request; do not turn
an unrun guest, device, or hardware check into a passing claim.

## Commits and pull requests

Use a descriptive branch and a concise Conventional Commit subject, for example:

```text
feat(shell): support Rust source execution
fix(storage): preserve workspace during updates
test: verify Cargo builds inside the guest
docs: explain the Linux system foundation
```

A pull request should explain the problem, resulting behavior, compatibility
impact, and verification. Include source/runtime prerequisites and a linked
issue when one exists. Check that the diff contains only intended files and
that application source, documentation, and relevant tests are included.

## Reporting bugs

Include the host OS and architecture, Python/Make/QEMU versions, the failed
command, and a minimal source example or serial transcript. From a booted guest,
include `rustc --version`, `cargo --version`, and relevant entries from
`/usr/lib/nixodria/packages.txt`. Say whether the problem affects a fresh image,
an existing runtime image, or the legacy build. Remove private text and secrets
from transcripts and diagnostics before publishing them.
