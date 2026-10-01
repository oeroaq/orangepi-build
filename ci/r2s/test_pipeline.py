"""Run-scoped artifact integrity and safe extraction regression tests."""
import copy
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import subprocess
import unittest
from unittest.mock import patch

import pipeline


class PipelineTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[2] / "_ci/tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temporary.name).resolve()
        self.original = pipeline.ROOT
        pipeline.ROOT = self.root
        self.context = {"build_id": "r2s-123-1", "run_id": "123", "builder_commit": "a" * 40,
                        "container_id": "sha256:" + "b" * 64, "architecture": "riscv64",
                        "source_date_epoch": 1700000000, "toolchain_sha256": "c" * 64,
                        "debian_snapshot": "20260928T000000Z", "sources_sha256": "d" * 64,
                        "sources": {"kernel": {"commit": "e" * 40}}}

    def tearDown(self):
        pipeline.ROOT = self.original
        self.assertIn((self.original / "_ci/tests").resolve(), self.root.parents)
        self.temporary.cleanup()

    def payload(self, role="kernel"):
        path = self.root / "payload" / role
        (path / "debs").mkdir(parents=True)
        (path / "debs/package.deb").write_bytes(b"sealed component bytes")
        return path

    def test_transfer_preserves_checksums_and_compilation_identity(self):
        source = self.payload()
        archive = pipeline.seal("kernel", source, self.context)
        received = pipeline.unpack("kernel", archive, self.context)
        self.assertEqual((received / "debs/package.deb").read_bytes(), b"sealed component bytes")
        pipeline.verify("kernel", received, self.context)

    def test_another_run_source_container_or_architecture_is_rejected(self):
        source = self.payload()
        pipeline.seal("kernel", source, self.context)
        for field, value in (("build_id", "r2s-999-1"), ("builder_commit", "f" * 40),
                             ("container_id", "sha256:" + "0" * 64), ("architecture", "amd64"),
                             ("source_date_epoch", 1700000001)):
            identity = copy.deepcopy(self.context)
            identity[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                pipeline.verify("kernel", source, identity)
        with self.assertRaises(ValueError):
            pipeline.verify("uboot", source, self.context)

    def test_tampered_missing_or_extra_payloads_are_rejected(self):
        source = self.payload()
        pipeline.seal("kernel", source, self.context)
        package = source / "debs/package.deb"
        original = package.read_bytes()
        package.write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            pipeline.verify("kernel", source, self.context)
        package.write_bytes(original)
        extra = source / "foreign.deb"
        extra.write_bytes(b"unaccounted old build")
        with self.assertRaises(ValueError):
            pipeline.verify("kernel", source, self.context)
        extra.unlink()
        package.unlink()
        with self.assertRaises(ValueError):
            pipeline.verify("kernel", source, self.context)

    def test_archive_cannot_escape_or_overwrite_through_links(self):
        for number, name in enumerate(("../escape", "/absolute", "safe")):
            archive = self.root / f"unsafe-{number}.tar"
            with tarfile.open(archive, "w") as output:
                entry = tarfile.TarInfo(name)
                if name == "safe":
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = "../escape"
                    output.addfile(entry)
                else:
                    entry.size = 1
                    output.addfile(entry, io.BytesIO(b"x"))
            with self.subTest(name=name), self.assertRaises(ValueError):
                pipeline.unpack("kernel", archive, self.context)
            self.assertFalse((self.root / "_ci/inbox/kernel").exists())

    def test_duplicate_archive_members_are_rejected_before_extraction(self):
        archive = self.root / "duplicate.tar"
        with tarfile.open(archive, "w") as output:
            for _ in range(2):
                entry = tarfile.TarInfo("package.deb")
                entry.size = 1
                output.addfile(entry, io.BytesIO(b"x"))
        with self.assertRaises(ValueError):
            pipeline.unpack("kernel", archive, self.context)

    def test_preparation_context_must_match_run_and_checkout(self):
        payload = self.root / "context-payload"
        (payload / "state").mkdir(parents=True)
        (payload / "state/context.json").write_text(json.dumps(self.context))
        archive = pipeline.seal("context", payload, self.context)
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "456"}), \
                patch.object(pipeline.subprocess, "check_output", return_value="a" * 40):
            with self.assertRaises(ValueError):
                pipeline.unpack("context", archive, build_id="r2s-123-1")

    def test_component_seal_is_not_silently_overwritten(self):
        source = self.payload()
        pipeline.seal("kernel", source, self.context)
        with self.assertRaises(FileExistsError):
            pipeline.seal("kernel", source, self.context)

    def test_image_transfer_includes_platform_package_for_the_final_release(self):
        state = self.root / "_ci/state"
        state.mkdir(parents=True)
        (state / "context.json").write_text(json.dumps(self.context))
        (state / "raw-image.sha256").write_text("d" * 64 + "  r2s-debian.img\n")
        images = self.root / "output/images/router"
        images.mkdir(parents=True)
        (images / "router.img.gz").write_bytes(b"image-transfer-fixture")
        (images / "router.img.gz.sha").write_text("fixture-checksum\n")
        debs = self.root / "output/debs"
        debs.mkdir()
        (debs / "r2s-platform_1.0_all.deb").write_bytes(b"native platform package")
        (self.root / "_ci/logs").mkdir()
        (self.root / "output/debug").mkdir()
        pipeline.capture("image")
        # Simulate a separate consumer with an empty output package directory.
        (debs / "r2s-platform_1.0_all.deb").unlink()
        pipeline.import_artifact("image")
        self.assertEqual((debs / "r2s-platform_1.0_all.deb").read_bytes(), b"native platform package")

    def test_packed_git_refs_survive_source_artifact_transfer(self):
        env = {**os.environ, "HOME": str(self.root), "GIT_CONFIG_NOSYSTEM": "1"}
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=self.root, env=env, text=True,
                                           stderr=subprocess.DEVNULL).strip()
        working = self.root / "working"
        git("init", str(working))
        (working / "source.c").write_text("int source;\n")
        git("-C", str(working), "add", "source.c")
        git("-C", str(working), "-c", "user.name=R2S test", "-c", "user.email=test@example.invalid", "commit", "-m", "source fixture")
        commit = git("-C", str(working), "rev-parse", "HEAD")
        payload = self.root / "source-payload"
        payload.mkdir()
        bare = payload / "kernel.git"
        git("clone", "--bare", str(working), str(bare))
        git("--git-dir", str(bare), "update-ref", "refs/heads/locked", commit)
        git("--git-dir", str(bare), "pack-refs", "--all", "--prune")
        self.assertTrue((bare / "refs").is_dir())
        self.assertFalse((bare / "refs/heads/locked").exists())
        archive = pipeline.seal("source-kernel", payload, self.context)
        received = pipeline.unpack("source-kernel", archive, self.context)
        destination = self.root / "copied/kernel.git"
        pipeline.copy_tree(received / "kernel.git", destination)
        self.assertTrue((destination / "refs").is_dir())
        self.assertEqual(git("--git-dir", str(destination), "rev-parse", "refs/heads/locked"), commit)
        git("--git-dir", str(destination), "fsck", "--no-dangling")


if __name__ == "__main__":
    unittest.main()
