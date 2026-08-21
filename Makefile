ASM ?= nasm
QEMU ?= qemu-system-i386
PYTHON ?= python3

BUILD_DIR := build
IMAGE := $(BUILD_DIR)/nixodria.img
RUNTIME_DIR := .nixodria
RUNTIME_IMAGE := $(RUNTIME_DIR)/nixodria.img

.PHONY: all check smoke runtime-image run clean

all: $(IMAGE)

$(BUILD_DIR):
	mkdir -p "$@"

$(IMAGE): src/boot.asm | $(BUILD_DIR)
	$(ASM) -f bin -Wall -Wno-reloc-abs-word -Werror "$<" -o "$@"

check: $(IMAGE)
	$(PYTHON) tests/check_image.py "$(IMAGE)"
	$(PYTHON) tests/check_runtime_image.py "$(IMAGE)" tools/prepare_runtime_image.py

smoke: check
	QEMU="$(QEMU)" $(PYTHON) tests/smoke.py "$(IMAGE)"

runtime-image: $(IMAGE)
	$(PYTHON) tools/prepare_runtime_image.py "$(IMAGE)" "$(RUNTIME_IMAGE)"

run: runtime-image
	$(QEMU) -accel tcg -boot a \
		-drive format=raw,file="$(RUNTIME_IMAGE)",if=floppy,cache=writethrough \
		-display none -serial stdio -monitor none -nic none \
		-no-reboot -no-shutdown

clean:
	rm -rf "$(BUILD_DIR)"
