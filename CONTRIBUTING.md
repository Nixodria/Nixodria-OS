# Contributing to Nixodria OS

Thank you for helping improve Nixodria OS. Contributions should preserve the
project's deliberately small, understandable 16-bit BIOS design and include
verification appropriate to the behavior being changed.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- Search the [issue tracker](https://github.com/Nixodria/Nixodria-OS/issues) for
  related reports or proposals.
- Open an issue before investing in a large change, especially one that alters
  the disk format, memory layout, BASIC language, or overall system scope.
- Keep each pull request focused on one problem. Small patches are easier to
  reason about in a memory-constrained real-mode system.

Bug fixes, tests, and documentation corrections are welcome without a prior
proposal when their scope and expected behavior are clear.

## Application policy: Nixodria BASIC

Every application contributed for inclusion in Nixodria OS must be written and
shared as editable source code in Nixodria's built-in flavor of BASIC. This
includes calculators, games, utilities, and every other user-facing program
intended to run within the OS.

This rule applies equally to everyone. The founder, maintainers, members of the
Nixodria organization, established contributors, and first-time newcomers all
follow the same application-language requirement. A title, role, or repository
permission does not create an exception.

Contributors are free to create, modify, refactor, and extend the BASIC source
code for their applications. The resulting application must still remain a
Nixodria BASIC program. For example, a contributed calculator must be BASIC
source run by Nixodria's interpreter, not calculator logic added directly to
`src/boot.asm`.

The existing built-in text editor is the only application exception. It was
created before Nixodria BASIC existed, so its assembly implementation may be
maintained, refactored, expanded, and enhanced. New features that are genuinely
part of the text editor remain covered by this exception. The exception may not
be used to fold an unrelated application or utility into the editor, and it
does not extend to any other new application.

This policy applies to applications, not to the core implementation of the OS.
The bootloader, kernel, shell, editor, BASIC interpreter, and platform support
may continue to use assembly or repository tooling where appropriate. If BASIC
lacks a capability that an application needs, propose and test that language or
runtime capability separately; do not bypass the policy by implementing the
application itself as native assembly or in another language.

Contributors must also share every change they make to Nixodria BASIC itself.
This includes changes to its syntax, language behavior, built-ins, interpreter,
or runtime support. Include those changes in the application's pull request or
in a linked prerequisite pull request that lands first, together with relevant
documentation and tests. An application must not depend on a private or
unpublished variant of Nixodria BASIC.

## Development environment

You need:

- NASM
- Python 3.10 or newer
- GNU Make
- QEMU with `qemu-system-i386`

On macOS with Homebrew, install the non-system dependencies with:

```sh
brew install nasm qemu
```

Build and verify a checkout before making changes:

```sh
make clean
make smoke
```

The Makefile defaults to `nasm`, `python3`, and `qemu-system-i386`. You can
override those commands when necessary, for example:

```sh
make PYTHON=python3.12 QEMU=/path/to/qemu-system-i386 smoke
```

Exact tool versions are not pinned. If a failure appears version-specific,
include the relevant version output in your issue or pull request.

## Repository layout

- `src/boot.asm` contains the boot sector, real-mode kernel, shell, editor,
  durable storage implementation, and BASIC interpreter.
- `tools/prepare_runtime_image.py` creates or safely refreshes the writable
  runtime image while preserving its storage sectors.
- `tests/check_image.py` validates the assembled image's static invariants.
- `tests/check_runtime_image.py` checks runtime-image creation, migration, and
  failure safety.
- `tests/smoke.py` boots temporary images in QEMU and exercises the system over
  its serial console.
- `build/nixodria.img` is the reproducible blank build output.
- `.nixodria/nixodria.img` is the local writable runtime image and can contain a
  user's saved document.

`build/`, `.nixodria/`, and Python cache directories are generated locally and
must not be committed.

## Making changes

### Preserve image and boot invariants

The current image is 18 sectors: one 512-byte BIOS boot sector, a seven-sector
kernel, and two five-sector save records. The first sector must retain its
`55 aa` BIOS signature, and a freshly built image must have blank storage
sectors.

If a change intentionally alters the image layout, update every affected
constant and assumption together in:

- `src/boot.asm`
- `tools/prepare_runtime_image.py`
- `tests/check_image.py`
- `tests/check_runtime_image.py`
- `tests/smoke.py`
- `README.md`

Preserve compatibility with existing runtime images when the layout changes, or
document and test an intentional migration path.

Do not weaken the size, bounds, checksum, or warning-as-error checks merely to
make a larger image build.

### Protect persistent data

Normal rebuilds replace only the runtime image's system sectors. They must not
overwrite its saved document. `make clean` intentionally removes `build/` but
leaves `.nixodria/nixodria.img` intact.

Changes to runtime-image handling or editor saves must remain fail-closed:
malformed images and symbolic links must not be overwritten, runtime files must
remain mode `0600` on POSIX hosts, failed writes must not be reported as
successful, and the previous verified save must remain recoverable after an
interrupted or corrupt write.

Do not attach a runtime image to a public issue without first checking its
contents; it may contain text saved in the editor.

### Follow the existing style

- Match the formatting and naming in the surrounding assembly or Python code.
- Keep real-mode memory use, register ownership, and BIOS side effects explicit.
- Comment constraints and non-obvious safety decisions, not line-by-line
  mechanics.
- Avoid adding a dependency when the standard library or existing toolchain is
  sufficient. Document any new required dependency.
- Update the README when commands, controls, supported BASIC syntax, image
  layout, or other user-visible behavior changes.

## Testing

The repository provides these validation targets:

- `make` assembles `build/nixodria.img` with NASM warnings treated as errors.
- `make check` validates the image layout and runtime-image preparation logic.
- `make smoke` runs `make check`, then exercises the shell, editor, BASIC
  interpreter, persistence, recovery, and write-failure behavior in QEMU.
- `make run` starts an interactive session on the headless COM1 serial console.
  Press Control-C to stop QEMU.

For code, tests, or tools, run the full local gate before opening a pull request:

```sh
make clean
make smoke
git diff --check
```

For documentation-only changes, run `git diff --check` and manually verify all
changed commands and links. If you cannot run an applicable check, say which
one was skipped and why in the pull request.

Behavior changes should include automated coverage in the closest existing
test. Prefer extending the QEMU smoke test for guest-visible behavior and the
focused Python checks for image-layout or runtime-image rules.

## Commits and pull requests

Create a short-lived branch with a descriptive name such as
`feature/basic-input`, `fix/storage-recovery`, or `docs/build-notes`.

Conventional Commit subjects are preferred. Keep them concise, imperative, and
focused on the project, for example:

```text
feat(editor): add cursor movement
fix(storage): preserve the previous valid record
docs: clarify QEMU setup
test: cover BASIC overflow handling
```

A pull request should include:

- A clear summary of the change and why it is needed.
- The user-visible and compatibility impact, if any.
- The exact validation commands run and their results.
- Tests for changed behavior.
- Documentation updates for changed commands or behavior.
- A linked issue when one exists.

Before submitting, confirm that:

- [ ] The diff contains only files relevant to the change.
- [ ] Generated images, runtime state, and cache files are not included.
- [ ] Every new application is implemented in Nixodria BASIC; only the existing
      pre-BASIC text editor and genuine enhancements to it are exempt.
- [ ] The complete application source and every required Nixodria BASIC change
      are included in this pull request or a linked prerequisite pull request.
- [ ] Image, memory, and persistent-storage invariants still hold.
- [ ] Applicable automated tests pass.
- [ ] `git diff --check` reports no whitespace errors.
- [ ] User-facing documentation is accurate.
- [ ] The contribution follows the Code of Conduct.

## Reporting bugs

Open a GitHub issue with the smallest reproducible example you can provide.
Include:

- The host operating system and architecture.
- NASM, Python, Make, and QEMU versions.
- The command you ran and complete error output or serial transcript.
- What you expected and what happened instead.
- Whether the problem occurs with a newly built blank image, an existing
  runtime image, or both.

Redact private text from serial transcripts and runtime-image diagnostics.
