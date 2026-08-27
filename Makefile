ASM ?= nasm
QEMU ?= qemu-system-i386
PYTHON ?= python3

BUILD_DIR := build
IMAGE := $(BUILD_DIR)/nixodria.img
BASE_IMAGE := $(BUILD_DIR)/nixodria-base.img
PRINT_MODULE := $(BUILD_DIR)/print.bin
BASIC_MODULE := $(BUILD_DIR)/basic.bin
RUNTIME_DIR := .nixodria
RUNTIME_IMAGE := $(RUNTIME_DIR)/nixodria.img
PACKAGE_LOCK := packages.lock.json
DEFAULT_PACKAGE_CATALOG := $(RUNTIME_DIR)/nixodria-packages.bin
PACKAGE_CATALOG ?= $(DEFAULT_PACKAGE_CATALOG)

.PHONY: all check smoke package-catalog verify-package-catalog runtime-image run clean

all: $(IMAGE)

$(BUILD_DIR):
	mkdir -p "$@"

$(BASE_IMAGE): src/boot.asm | $(BUILD_DIR)
	$(ASM) -f bin -Wall -Wno-reloc-abs-word -Werror "$<" -o "$@"

$(PRINT_MODULE): src/print.asm | $(BUILD_DIR)
	$(ASM) -f bin -Wall -Wno-reloc-abs-word -Werror "$<" -o "$@"

$(BASIC_MODULE): src/basic.asm | $(BUILD_DIR)
	$(ASM) -f bin -Wall -Wno-reloc-abs-word -Werror "$<" -o "$@"

ifeq ($(PACKAGE_CATALOG),$(DEFAULT_PACKAGE_CATALOG))
PACKAGE_CATALOG_CHECK_MODE := pinned
verify-package-catalog: $(PACKAGE_LOCK) tools/fetch_package_catalog.py
	$(PYTHON) tools/fetch_package_catalog.py "$(PACKAGE_LOCK)" \
		"$(DEFAULT_PACKAGE_CATALOG)"
PACKAGE_CATALOG_PREREQUISITES := verify-package-catalog
package-catalog: verify-package-catalog
else
PACKAGE_CATALOG_CHECK_MODE := override
.PHONY: force-package-catalog
PACKAGE_CATALOG_PREREQUISITES := $(PACKAGE_CATALOG) force-package-catalog
force-package-catalog:
package-catalog: $(PACKAGE_CATALOG)
endif

$(IMAGE): $(BASE_IMAGE) $(PRINT_MODULE) $(BASIC_MODULE) \
	$(PACKAGE_CATALOG_PREREQUISITES) \
	tools/build_image.py
	$(PYTHON) tools/build_image.py "$(BASE_IMAGE)" "$(PRINT_MODULE)" \
		"$(BASIC_MODULE)" "$(PACKAGE_CATALOG)" "$@"

check: $(IMAGE)
	$(PYTHON) tests/check_package_catalog.py "$(PACKAGE_CATALOG_CHECK_MODE)" \
		"$(PACKAGE_LOCK)" \
		"$(PACKAGE_CATALOG)" tools/fetch_package_catalog.py tools/build_image.py
	$(PYTHON) tests/check_image.py "$(IMAGE)"
	$(PYTHON) tests/check_runtime_image.py "$(IMAGE)" tools/prepare_runtime_image.py

smoke: check
	QEMU="$(QEMU)" $(PYTHON) tests/smoke.py "$(IMAGE)"
	QEMU="$(QEMU)" $(PYTHON) tests/check_native_print.py "$(IMAGE)"

runtime-image: $(IMAGE)
	$(PYTHON) tools/prepare_runtime_image.py "$(IMAGE)" "$(RUNTIME_IMAGE)"

run: runtime-image
	$(QEMU) -accel tcg -boot a \
		-drive format=raw,file="$(RUNTIME_IMAGE)",if=floppy,cache=writethrough \
		-display none -serial stdio -monitor none \
		-netdev user,id=nixnet,ipv6=off \
		-device ne2k_isa,netdev=nixnet,iobase=0x300,irq=9,mac=52:54:00:12:34:56 \
		-no-reboot -no-shutdown

clean:
	rm -rf "$(BUILD_DIR)"
