"""Offline regression checks for pinned sources and cache reuse."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import prepare


class PreparationTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[2] / "_ci/tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temporary.name).resolve()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(prepare, "ROOT", self.root))
        self.stack.enter_context(patch.object(prepare, "STATE", self.root / "state"))
        self.stack.enter_context(patch.object(prepare, "CACHE", self.root / "cache"))
        self.stack.enter_context(patch.dict(os.environ, {"HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1", "GITHUB_OUTPUT": str(self.root / "outputs")}))
        # Only test-local Git config is written or read.
        self.git("config", "--global", "init.defaultBranch", "main")
        self.git("config", "--global", "user.name", "CI test")
        self.git("config", "--global", "user.email", "ci-test@example.invalid")

    def tearDown(self):
        self.stack.close()
        parent = Path(__file__).resolve().parents[2] / "_ci/tests"
        self.assertIn(parent.resolve(), self.root.parents)
        self.temporary.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd or self.root,
                                       text=True, stderr=subprocess.DEVNULL).strip()

    def remote(self, name):
        directory = self.root / name
        directory.mkdir()
        self.git("init", cwd=directory)
        (directory / "version").write_text("first\n")
        self.git("add", "version", cwd=directory)
        self.git("commit", "-m", "first", cwd=directory)
        first = self.git("rev-parse", "HEAD", cwd=directory)
        (directory / "version").write_text("second\n")
        self.git("commit", "-am", "second", cwd=directory)
        second = self.git("rev-parse", "HEAD", cwd=directory)
        return directory, first, second

    def test_rejects_invalid_snapshot_before_network(self):
        with patch.object(prepare, "git") as git:
            for snapshot in ("latest", "20261328T000000Z", "20260928T000000Z;touch file"):
                with self.assertRaises(ValueError):
                    prepare.freeze(snapshot, "", "")
            git.assert_not_called()

    def test_locks_full_hashes_and_refuses_overwrite(self):
        def fake_git(*args, **kwargs):
            if args[:1] == ("ls-remote",):
                return "a" * 40 + "\t" + args[-1]
            return "b" * 40

        with patch.object(prepare, "git", side_effect=fake_git), contextlib.redirect_stdout(io.StringIO()):
            prepare.freeze("20260928T000000Z", "c" * 40, "d" * 40)
            before = (prepare.STATE / "sources.lock.json").read_bytes()
            with self.assertRaises(FileExistsError):
                prepare.freeze("20260929T000000Z", "c" * 40, "d" * 40)
            self.assertEqual(before, (prepare.STATE / "sources.lock.json").read_bytes())
        manifest = json.loads(before)
        self.assertEqual(manifest["sources"]["kernel"]["commit"], "c" * 40)
        self.assertEqual(manifest["sources"]["uboot"]["commit"], "d" * 40)
        self.assertIn("sources_key=", (self.root / "outputs").read_text())

    def test_rejects_short_or_injected_commits(self):
        for commit in ("abcdef0", "main", "a" * 40 + ";id"):
            with self.assertRaises(ValueError):
                prepare.freeze("20260928T000000Z", commit, "b" * 40)

    def test_stale_git_cache_checks_out_requested_commit(self):
        remote, first, second = self.remote("remote")
        url = remote.as_uri()
        old_tree = prepare.checkout_source("example", {"url": url, "commit": first}, "tree-old")
        new_tree = prepare.checkout_source("example", {"url": url, "commit": second}, "tree-new")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=old_tree), first)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=new_tree), second)
        self.assertEqual((new_tree / "version").read_text(), "second\n")
        with self.assertRaises(ValueError):
            prepare.checkout_source("example", {"url": "unexpected", "commit": second}, "tree-other")

    def test_wiringop_submodule_uses_gitlink_not_branch_head(self):
        wiring, first, second = self.remote("wiring")
        parent, _, _ = self.remote("python")
        (parent / ".gitmodules").write_text(
            '[submodule "wiringOP"]\n\tpath = wiringOP\n\turl = ' + wiring.as_uri() + '\n')
        self.git("add", ".gitmodules", cwd=parent)
        self.git("update-index", "--add", "--cacheinfo", "160000", first, "wiringOP", cwd=parent)
        self.git("commit", "-m", "submodule", cwd=parent)
        commit = self.git("rev-parse", "HEAD", cwd=parent)
        manifest = {"sources": {
            "kernel": {"url": wiring.as_uri(), "commit": first},
            "uboot": {"url": wiring.as_uri(), "commit": first},
            "wiringOP": {"url": wiring.as_uri(), "commit": second},
            "wiringOP-Python": {"url": parent.as_uri(), "commit": commit}}}
        (prepare.STATE).mkdir()
        with patch.dict(prepare.SOURCES, {"wiringOP": (wiring.as_uri(), "main")}):
            (prepare.STATE / "sources.lock.json").write_text(json.dumps(manifest))
            prepare.prepare_sources()
        nested = self.root / "external/cache/sources/wiringOP-Python/next/wiringOP"
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=nested), first)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.root / "external/cache/sources/wiringOP/next"), second)
        self.assertTrue((prepare.STATE / "sources.ready").exists())

    def test_rejects_paths_resolving_outside_checkout(self):
        (self.root / "escape").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            prepare.write_new(self.root / "escape/file", "not written")
        with self.assertRaises(ValueError):
            prepare.inside(self.root / "../outside")


if __name__ == "__main__":
    unittest.main()
