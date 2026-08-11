ASM ?= nasm
QEMU ?= qemu-system-i386
PYTHON ?= python3

BUILD_DIR := build
IMAGE := $(BUILD_DIR)/nixodria.img

.PHONY: all check smoke run clean

all: $(IMAGE)

$(BUILD_DIR):
	mkdir -p "$@"

$(IMAGE): src/boot.asm | $(BUILD_DIR)
	$(ASM) -f bin -Wall -Wno-reloc-abs-word -Werror "$<" -o "$@"

check: $(IMAGE)
	$(PYTHON) tests/check_image.py "$(IMAGE)"

smoke: check
	QEMU="$(QEMU)" $(PYTHON) tests/smoke.py "$(IMAGE)"

run: $(IMAGE)
	$(QEMU) -accel tcg -boot a \
		-drive format=raw,file="$(IMAGE)",if=floppy \
		-display none -serial stdio -monitor none \
		-no-reboot -no-shutdown

clean:
	rm -rf "$(BUILD_DIR)"
