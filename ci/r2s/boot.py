#!/usr/bin/env python3
"""Install and validate the actual boot script and early RCPU firmware."""
import argparse
import gzip
from pathlib import Path
import struct
import zlib

ROOT = Path(__file__).resolve().parents[2]
BOOT_COMMAND = "booti ${kernel_addr_r} ${ramdisk_addr_r} ${fdt_addr_r}"
FIRMWARE = ROOT / "external/packages/bsp/ky/usr/lib/firmware/esos.elf"


def watchdog_bootscript(original):
    fragment = Path(__file__).with_name("boot-watchdogs.cmd").read_text()
    lines = original.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == BOOT_COMMAND]
    if len(matches) != 1:
        raise ValueError("Expected exactly one vendor kernel handoff")
    if fragment in original:
        return original
    if "# R2S watchdog handoff:" in original:
        raise ValueError("Unexpected existing watchdog handoff")
    lines[matches[0]] = fragment + "\n" + lines[matches[0]]
    return "".join(lines)


def legacy_payload(data):
    if len(data) < 64:
        raise ValueError("Truncated legacy U-Boot image")
    magic, hcrc, _, size, _, _, dcrc = struct.unpack_from(">7I", data)
    header = data[:4] + b"\0" * 4 + data[8:64]
    payload = data[64:]
    if (magic != 0x27051956 or zlib.crc32(header) != hcrc or len(payload) != size
            or zlib.crc32(payload) != dcrc):
        raise ValueError("Invalid legacy U-Boot image header, size or checksum")
    return payload


def initramfs_firmware(data):
    archive = gzip.decompress(legacy_payload(data))
    position = 0
    firmware = []
    while position < len(archive):
        while position < len(archive) and archive[position] == 0:
            position += 1
        if position == len(archive):
            break
        header = archive[position:position + 110]
        if len(header) != 110 or header[:6] not in (b"070701", b"070702"):
            raise ValueError("Invalid initramfs cpio header")
        fields = [int(header[index:index + 8], 16) for index in range(6, 110, 8)]
        mode, size, name_size = fields[1], fields[6], fields[11]
        start = position + 110
        end = start + name_size
        if name_size < 1 or end > len(archive) or archive[end - 1] != 0:
            raise ValueError("Invalid initramfs cpio filename")
        name = archive[start:end - 1].decode()
        position = (end + 3) & ~3
        end = position + size
        if end > len(archive):
            raise ValueError("Truncated initramfs cpio file")
        if name.startswith("./"):
            name = name[2:]
        if name in ("lib/firmware/esos.elf", "usr/lib/firmware/esos.elf"):
            if mode & 0o170000 != 0o100000:
                raise ValueError("RCPU firmware in initramfs must be a regular file")
            firmware.append(archive[position:end])
        position = (end + 3) & ~3
    # Legacy family hooks may populate /lib as well as /usr/lib before the
    # initramfs usrmerge step. Every available copy must be identical.
    if not firmware or any(value != firmware[0] for value in firmware):
        raise ValueError("Initramfs RCPU firmware is missing or has conflicting copies")
    return firmware[0]


def verify(root):
    expected = watchdog_bootscript((ROOT / "external/config/bootscripts/boot-ky.cmd").read_text())
    installed = (root / "boot/boot.cmd").read_text()
    if installed != expected:
        raise ValueError("Installed boot script lacks the expected watchdog handoff")
    script = legacy_payload((root / "boot/boot.scr").read_bytes())
    encoded = installed.encode()
    if len(script) < 8 or struct.unpack_from(">2I", script) != (len(encoded), 0) or script[8:] != encoded:
        raise ValueError("boot.scr does not execute the validated boot.cmd")
    expected_firmware = FIRMWARE.read_bytes()
    if not expected_firmware.startswith(b"\x7fELF"):
        raise ValueError("Versioned RCPU firmware is not an ELF image")
    if (root / "usr/lib/firmware/esos.elf").read_bytes() != expected_firmware:
        raise ValueError("Installed RCPU firmware differs from the versioned BSP")
    if initramfs_firmware((root / "boot/uInitrd").read_bytes()) != expected_firmware:
        raise ValueError("Early RCPU firmware differs from the installed/versioned firmware")
    print("Boot validation passed: watchdog handoff, boot.scr and matching RCPU firmware in rootfs/initramfs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "verify"))
    parser.add_argument("root")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not root.is_relative_to(ROOT):
        raise ValueError("Image root is outside the build workspace")
    if args.command == "install":
        path = root / "boot/boot.cmd"
        path.write_text(watchdog_bootscript(path.read_text()))
    else:
        verify(root)


if __name__ == "__main__":
    main()
