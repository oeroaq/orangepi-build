"""Regressions for inherited watchdogs and firmware needed before rootfs."""
import gzip
from pathlib import Path
import struct
import subprocess
import unittest
import zlib

import boot


def legacy_image(payload):
    header = bytearray(64)
    struct.pack_into(">7I", header, 0, 0x27051956, 0, 0, len(payload), 0, 0, zlib.crc32(payload))
    struct.pack_into(">I", header, 4, zlib.crc32(header))
    return bytes(header) + payload


def cpio_file(name, data=b"", mode=0o100644):
    encoded = name.encode() + b"\0"
    fields = (1, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(encoded), 0)
    record = b"070701" + b"".join(f"{value:08x}".encode() for value in fields) + encoded
    record += b"\0" * (-len(record) % 4)
    record += data
    return record + b"\0" * (-len(record) % 4)


class BootTests(unittest.TestCase):
    def initrd(self, entries):
        archive = b"".join(cpio_file(*entry) for entry in entries) + cpio_file("TRAILER!!!")
        return legacy_image(gzip.compress(archive))

    def test_firmware_is_found_in_usrmerged_and_legacy_initramfs(self):
        firmware = b"\x7fELF-RCPU"
        for name in ("usr/lib/firmware/esos.elf", "./lib/firmware/esos.elf"):
            with self.subTest(name=name):
                self.assertEqual(boot.initramfs_firmware(self.initrd([(name, firmware)])), firmware)

    def test_missing_conflicting_or_symlink_firmware_is_rejected(self):
        for entries in ([("init", b"#!/bin/sh")],
                        [("lib/firmware/esos.elf", b"old"), ("usr/lib/firmware/esos.elf", b"new")],
                        [("lib/firmware/esos.elf", b"elsewhere", 0o120777)]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                boot.initramfs_firmware(self.initrd(entries))

    def test_corruption_and_truncation_of_boot_payloads_are_rejected(self):
        original = legacy_image(b"boot script\n")
        self.assertEqual(boot.legacy_payload(original), b"boot script\n")
        for bad in (original[:50], original[:-1], original + b"extra",
                    original[:8] + b"\xff" + original[9:], original[:-1] + b"X"):
            with self.subTest(size=len(bad)), self.assertRaises(ValueError):
                boot.legacy_payload(bad)

    def test_watchdog_handoff_is_before_kernel_and_idempotent(self):
        original = "echo loading\n" + boot.BOOT_COMMAND
        result = boot.watchdog_bootscript(original)
        self.assertLess(result.index("wdt stop"), result.index(boot.BOOT_COMMAND))
        self.assertEqual(boot.watchdog_bootscript(result), result)
        for bad in ("no kernel command\n", original + "\n" + boot.BOOT_COMMAND):
            with self.assertRaises(ValueError):
                boot.watchdog_bootscript(bad)

    def handoff(self, scenario):
        # U-Boot's fragment uses the common if/for command subset. Mock the
        # commands to test stop/failure flow without touching a serial device.
        commands = r'''
scenario=$1
wdt() {
    case "$1" in
        list) test "$scenario" != unavailable ;;
        dev)
            case "$2" in
                PMIC_WDT|watchdog@D4080000) current=$2; return 0 ;;
                *) return 1 ;;
            esac ;;
        stop)
            echo "STOP:$current"
            test "$scenario" != stop-fails ;;
        *) return 1 ;;
    esac
}
setenv() { :; }
'''
        fragment = Path(boot.__file__).with_name("boot-watchdogs.cmd").read_text()
        return subprocess.run(["bash", "-c", commands + fragment + '\necho KERNEL_HANDOFF\n',
                               "handoff-test", scenario], text=True, capture_output=True)

    def test_both_watchdogs_are_stopped_before_kernel(self):
        result = self.handoff("normal")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("PMIC_WDT", "watchdog@D4080000"):
            self.assertLess(result.stdout.index("STOP:" + name), result.stdout.index("KERNEL_HANDOFF"))

    def test_watchdog_stop_failure_aborts_instead_of_entering_kernel(self):
        result = self.handoff("stop-fails")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot stop watchdog PMIC_WDT", result.stdout)
        self.assertNotIn("KERNEL_HANDOFF", result.stdout)

    def test_bootloader_without_watchdog_command_reports_and_continues(self):
        result = self.handoff("unavailable")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("watchdog command unavailable", result.stdout)
        self.assertIn("KERNEL_HANDOFF", result.stdout)
        self.assertNotIn("STOP:", result.stdout)


if __name__ == "__main__":
    unittest.main()
