#!/usr/bin/env python3
"""Export a standalone Nixodria virtual machine for UTM on macOS."""

import argparse
import ctypes
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMAGE = ROOT / "build/system/nixodria.qcow2"
DEFAULT_OUTPUT = ROOT / ".nixodria/Nixodria.utm"


class ExportError(RuntimeError):
    pass


def configuration() -> dict:
    """Build the version 4 property list understood by UTM 4.7.5.

    Schema: https://github.com/utmapp/UTM/tree/v4.7.5/Configuration
    Enum values are case sensitive; empty device arrays are required fields.
    """
    mac = bytearray(secrets.token_bytes(6))
    mac[0] = (mac[0] | 0x02) & 0xFE  # Locally administered, unicast address.
    return {
        "ConfigurationVersion": 4,
        "Backend": "QEMU",
        "Information": {
            "Name": "Nixodria OS 2",
            "UUID": str(uuid.uuid4()).upper(),
            "Icon": "linux",
            "IconCustom": False,
            "Notes": "Rust development environment. Type help to begin; halt to shut down.",
        },
        "System": {
            "Architecture": "x86_64",
            "Target": "pc",
            "CPU": "default",
            "CPUFlagsAdd": [],
            "CPUFlagsRemove": [],
            "CPUCount": 2,
            "ForceMulticore": False,
            "MemorySize": 2048,
            "JITCacheSize": 0,
        },
        "QEMU": {
            "DebugLog": False,
            "UEFIBoot": False,
            "RNGDevice": False,
            "BalloonDevice": False,
            "TPMDevice": False,
            "Hypervisor": False,
            "TSO": False,
            "RTCLocalTime": False,
            "PS2Controller": False,
            "AdditionalArguments": [],
        },
        "Input": {
            "UsbBusSupport": "Disabled",
            "UsbSharing": False,
            "MaximumUsbShare": 0,
        },
        "Sharing": {
            "DirectoryShareMode": "None",
            "DirectoryShareReadOnly": False,
            "ClipboardSharing": False,
        },
        "Display": [],
        "Drive": [{
            "ImageName": "nixodria.qcow2",
            "ImageType": "Disk",
            "Interface": "VirtIO",
            "InterfaceVersion": 1,
            "Identifier": str(uuid.uuid4()).upper(),
            "ReadOnly": False,
        }],
        "Network": [{
            "Mode": "Emulated",
            "Hardware": "virtio-net-pci",
            "MacAddress": ":".join(f"{byte:02X}" for byte in mac),
            "IsolateFromHost": False,
            "PortForward": [],
        }],
        "Serial": [{
            "Mode": "Terminal",
            "Target": "Auto",
            "Terminal": {
                "ForegroundColor": "#ffffff",
                "BackgroundColor": "#000000",
                "Font": "Menlo",
                "FontSize": 14,
                "CursorBlink": True,
            },
        }],
        "Sound": [],
    }


def run_qemu_img(qemu_img: str, *arguments: str) -> str:
    result = subprocess.run([qemu_img, *arguments], capture_output=True, text=True)
    if result.returncode:
        details = result.stderr.strip() or result.stdout.strip()
        raise ExportError(f"qemu-img {arguments[0]} failed: {details}")
    return result.stdout


def inspect_image(qemu_img: str, image: Path, *, standalone: bool = False) -> None:
    try:
        info = json.loads(run_qemu_img(qemu_img, "info", "--output=json", str(image)))
    except json.JSONDecodeError as error:
        raise ExportError(f"qemu-img returned invalid image information for {image}") from error
    if info.get("format") != "qcow2":
        raise ExportError(f"image must use qcow2 format: {image}")
    if info.get("virtual-size", 0) <= 0:
        raise ExportError(f"image has no virtual disk capacity: {image}")
    if standalone and info.get("backing-filename"):
        raise ExportError(f"exported image unexpectedly depends on a backing file: {image}")


def publish_exclusively(temporary: Path, output: Path) -> None:
    """Atomically rename a completed bundle without replacing any existing path."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        arguments = (os.fsencode(temporary), os.fsencode(output), 0x00000004)
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint]
        arguments = (-100, os.fsencode(temporary), -100, os.fsencode(output), 1)
    else:
        raise ExportError("safe bundle publication requires macOS or Linux with renameat2")
    rename.restype = ctypes.c_int
    if rename(*arguments) != 0:
        error = ctypes.get_errno()
        if os.path.lexists(output):
            raise ExportError(f"destination already exists; choose a new --output: {output}")
        raise ExportError(f"cannot publish {output}: {os.strerror(error)}")


def export(image: Path, output: Path) -> Path:
    image = image.expanduser().resolve()
    # Do not resolve the last component: a dangling destination symlink must
    # still count as an existing path rather than redirecting the export.
    output = Path(os.path.abspath(output.expanduser()))
    if os.path.lexists(output):
        raise ExportError(f"destination already exists; choose a new --output: {output}")
    if output.suffix != ".utm":
        raise ExportError(f"destination must end in .utm: {output}")
    if not image.is_file():
        raise ExportError(f"image not found: {image}; run make first or pass --image")
    qemu_img = shutil.which(os.environ.get("QEMU_IMG", "qemu-img"))
    if not qemu_img:
        raise ExportError("qemu-img is required; on macOS install it with: brew install qemu")
    inspect_image(qemu_img, image)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        data = temporary / "Data"
        data.mkdir(mode=0o700)
        disk = data / "nixodria.qcow2"
        # Conversion flattens backing chains. Do not use -U: active guest disk
        # locks must be honored so the exported filesystem remains consistent.
        run_qemu_img(qemu_img, "convert", "-f", "qcow2", "-O", "qcow2",
                     str(image), str(disk))
        disk.chmod(0o600)
        inspect_image(qemu_img, disk, standalone=True)
        run_qemu_img(qemu_img, "check", "-f", "qcow2", str(disk))
        with (temporary / "config.plist").open("wb") as config_file:
            os.fchmod(config_file.fileno(), 0o600)
            plistlib.dump(configuration(), config_file, sort_keys=False)
        publish_exclusively(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE,
                        help="source qcow2 image (default: build/system/nixodria.qcow2)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="new .utm bundle (default: .nixodria/Nixodria.utm)")
    args = parser.parse_args()
    try:
        output = export(args.image, args.output)
    except (ExportError, OSError) as error:
        print(f"utm: {error}", file=sys.stderr)
        return 1
    print(f"utm: created {output}")
    print("Open this bundle in UTM, then press Play. Use halt for a clean shutdown.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
