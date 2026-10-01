"""SDK relocation and archive containment regressions, including the vendor link."""
import hashlib
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest

import toolchain


P = toolchain.PREFIX


def entry(name, kind=tarfile.REGTYPE, target=None, data=b"payload", mode=0o644):
    item = tarfile.TarInfo(name)
    item.type = kind
    item.mode = mode
    if target is not None:
        item.linkname = target
    if kind == tarfile.REGTYPE:
        item.size = len(data)
    return item, data


class ToolchainTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[2] / "_ci/tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temporary.name).resolve()
        self.original = toolchain.ROOT
        toolchain.ROOT = self.root

    def tearDown(self):
        toolchain.ROOT = self.original
        self.assertIn((self.original / "_ci/tests").resolve(), self.root.parents)
        self.temporary.cleanup()

    def archive(self, records, name="sdk.tar.xz"):
        archive = self.root / name
        with tarfile.open(archive, "w:xz") as output:
            for item, data in records:
                output.addfile(item, io.BytesIO(data) if item.isfile() else None)
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def test_actual_vendor_install_prefix_is_relocated_inside_sdk(self):
        archive, digest = self.archive([
            entry(P + "/sysroot/lib/gcc", tarfile.SYMTYPE, "/home/ci/gcc_linux_install/lib/gcc"),
            entry(P + "/lib/gcc/plugin", data=b"real vendor target"),
            entry(P + "/bin/riscv64-unknown-linux-gnu-gcc", data=b"compiler", mode=0o755),
        ])
        target = toolchain.extract(archive, self.root / "toolchains", digest)
        alias = target / "sysroot/lib/gcc"
        self.assertEqual(os.readlink(alias), "../../lib/gcc")
        self.assertEqual((alias / "plugin").read_bytes(), b"real vendor target")
        self.assertIn(target, alias.resolve().parents)
        self.assertEqual((target / "bin/riscv64-unknown-linux-gnu-gcc").stat().st_mode & 0o777, 0o755)

    def test_guest_absolute_library_symlink_does_not_resolve_host_lib(self):
        archive, digest = self.archive([
            entry(P + "/sysroot/lib/libc.so", tarfile.SYMTYPE, "/lib64/libc.so.6"),
            entry(P + "/sysroot/lib64/libc.so.6", data=b"riscv libc"),
        ])
        target = toolchain.extract(archive, self.root / "toolchains", digest)
        alias = target / "sysroot/lib/libc.so"
        self.assertEqual(os.readlink(alias), "../lib64/libc.so.6")
        self.assertEqual(alias.read_bytes(), b"riscv libc")

    def test_relative_symlinks_and_archive_root_hardlinks_are_distinct(self):
        archive, digest = self.archive([
            entry(P + "/bin/gcc", tarfile.SYMTYPE, "../libexec/gcc"),
            entry(P + "/libexec/gcc", data=b"executable", mode=0o755),
            entry(P + "/bin/gcc-hard", tarfile.LNKTYPE, P + "/libexec/gcc"),
        ])
        target = toolchain.extract(archive, self.root / "toolchains", digest)
        self.assertEqual((target / "bin/gcc").read_bytes(), b"executable")
        self.assertEqual((target / "bin/gcc-hard").stat().st_ino, (target / "libexec/gcc").stat().st_ino)

    def test_hardlink_chain_can_precede_its_data(self):
        archive, digest = self.archive([
            entry(P + "/bin/b", tarfile.LNKTYPE, P + "/bin/a"),
            entry(P + "/bin/a", tarfile.LNKTYPE, P + "/libexec/gcc"),
            entry(P + "/libexec/gcc", data=b"same bytes"),
        ])
        target = toolchain.extract(archive, self.root / "toolchains", digest)
        self.assertEqual((target / "bin/b").read_bytes(), b"same bytes")

    def test_external_absolute_paths_and_relative_escapes_are_rejected(self):
        for number, link in enumerate(("/etc/passwd", "/home/ci/other/lib/gcc",
                                       "/home/ci/gcc_linux_install/../../outside", "../../../outside")):
            archive, digest = self.archive([entry(P + "/bin/link", tarfile.SYMTYPE, link)], f"unsafe-{number}.tar.xz")
            with self.subTest(link=link), self.assertRaisesRegex(ValueError, "link|member"):
                toolchain.extract(archive, self.root / f"out-{number}", digest)
            self.assertFalse((self.root / f"out-{number}" / P).exists())

    def test_transitive_escape_and_link_cycles_are_rejected(self):
        records = [entry(P + "/a", tarfile.SYMTYPE, "b"), entry(P + "/b", tarfile.SYMTYPE, "../../outside")]
        with self.assertRaises(ValueError):
            toolchain.validate_members([item for item, _ in records])
        records = [entry(P + "/a", tarfile.SYMTYPE, "b"), entry(P + "/b", tarfile.SYMTYPE, "a")]
        with self.assertRaisesRegex(ValueError, "Cyclic"):
            toolchain.validate_members([item for item, _ in records])

    def test_data_is_never_extracted_through_an_archive_symlink(self):
        archive, digest = self.archive([
            entry(P + "/a", tarfile.SYMTYPE, "safe"),
            entry(P + "/a/data", data=b"would traverse a symlink"),
        ])
        with self.assertRaisesRegex(ValueError, "traverses"):
            toolchain.extract(archive, self.root / "toolchains", digest)

    def test_duplicate_special_members_missing_hardlinks_and_hash_mismatch(self):
        invalid = ([entry(P + "/a"), entry(P + "/a")],
                   [entry(P + "/device", tarfile.CHRTYPE)],
                   [entry(P + "/a", tarfile.LNKTYPE, P + "/missing")])
        for number, records in enumerate(invalid):
            archive, digest = self.archive(records, f"invalid-{number}.tar.xz")
            with self.subTest(number=number), self.assertRaises(ValueError):
                toolchain.extract(archive, self.root / f"out-{number}", digest)
        archive, digest = self.archive([entry(P + "/file")], "hash.tar.xz")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            toolchain.extract(archive, self.root / "hash-out", "0" * 64)
        self.assertFalse((self.root / "hash-out").exists())

    def test_existing_compiler_is_never_overwritten(self):
        archive, digest = self.archive([entry(P + "/file")])
        existing = self.root / "toolchains" / P
        existing.mkdir(parents=True)
        (existing / "user-work").write_text("keep")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            toolchain.extract(archive, existing.parent, digest)
        self.assertEqual((existing / "user-work").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
