ASM ?= nasm
QEMU ?= qemu-system-i386
PYTHON ?= python3

BUILD_DIR := build
IMAGE := $(BUILD_DIR)/nixodria.img
BASE_IMAGE := $(BUILD_DIR)/nixodria-base.img
PRINT_MODULE := $(BUILD_DIR)/print.bin
RUNTIME_DIR := .nixodria
RUNTIME_IMAGE := $(RUNTIME_DIR)/nixodria.img

.PHONY: all check smoke runtime-image run clean

all: $(IMAGE)

$(BUILD_DIR):
	mkdir -p "$@"

$(BASE_IMAGE): src/boot.asm | $(BUILD_DIR)
	$(ASM) -f bin -Wall -Wno-reloc-abs-word -Werror "$<" -o "$@"

$(PRINT_MODULE): src/print.asm | $(BUILD_DIR)
	$(ASM) -f bin -Wall -Wno-reloc-abs-word -Werror "$<" -o "$@"

$(IMAGE): $(BASE_IMAGE) $(PRINT_MODULE) tools/build_image.py
	$(PYTHON) tools/build_image.py "$(BASE_IMAGE)" "$(PRINT_MODULE)" "$@"

check: $(IMAGE)
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
