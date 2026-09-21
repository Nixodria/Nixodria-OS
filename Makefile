PYTHON ?= python3
QEMU ?= qemu-system-x86_64
QEMU_IMG ?= qemu-img
OUTPUT ?= legacy-files
export QEMU QEMU_IMG

.PHONY: all check smoke run runtime-image update export-legacy import-legacy legacy legacy-smoke clean

all:
	$(PYTHON) tools/build_system.py build

check: all
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'
	$(QEMU_IMG) check build/system/nixodria.qcow2

smoke: check
	$(PYTHON) tests/smoke_rust.py build/system/nixodria.qcow2
	$(PYTHON) tests/smoke_legacy_import.py build/system/nixodria.qcow2

runtime-image: all
	$(PYTHON) tools/build_system.py runtime

run: runtime-image
	$(PYTHON) tools/build_system.py run

update: all
	$(PYTHON) tools/build_system.py update

export-legacy:
	$(PYTHON) tools/export_legacy_files.py .nixodria/nixodria.img "$(OUTPUT)"

import-legacy: runtime-image
	$(PYTHON) tools/import_legacy_files.py .nixodria/nixodria.img .nixodria/nixodria-rust.qcow2

legacy:
	$(MAKE) -f tools/legacy.mk QEMU=qemu-system-i386 all

legacy-smoke:
	$(MAKE) -f tools/legacy.mk QEMU=qemu-system-i386 smoke

clean:
	rm -rf build target
