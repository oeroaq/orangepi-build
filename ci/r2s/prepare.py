#!/usr/bin/env python3
"""Freeze public sources and prepare disposable CI checkouts, never build outputs."""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "_ci/state"
CACHE = ROOT / "_ci/cache"
TOOLCHAIN = "ky-toolchain-linux-glibc-x86_64-v1.0.1"
TOOLCHAIN_MD5 = "15c8eb90a4ba8139a034149e6fd74528"
TOOLCHAIN_URL = f"http://www.iplaystore.cn/upload/_toolchain/{TOOLCHAIN}.tar.xz"
SOURCES = {
    "kernel": ("https://github.com/orangepi-xunlong/linux-orangepi.git", "orange-pi-6.6-ky"),
    "uboot": ("https://github.com/orangepi-xunlong/u-boot-orangepi.git", "v2022.10-ky"),
    "firmware": ("https://github.com/orangepi-xunlong/firmware.git", "master"),
}


def inside(path):
    path = Path(path).resolve()
    if ROOT not in path.parents:
        raise ValueError(f"Path outside checkout: {path}")
    return path


def git(*args, cwd=None):
    cwd = ROOT if cwd is None else cwd
    return subprocess.check_output(["git", *args], cwd=inside(cwd) if cwd != ROOT else ROOT,
                                   text=True).strip()


def write_new(path, content):
    path = inside(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A source update must never silently rewrite an existing lock.
    with path.open("x") as stream:
        stream.write(content)


def freeze(snapshot, kernel_commit, uboot_commit):
    if not re.fullmatch(r"\d{8}T\d{6}Z", snapshot):
        raise ValueError("debian_snapshot must have format YYYYMMDDTHHMMSSZ")
    datetime.datetime.strptime(snapshot, "%Y%m%dT%H%M%SZ")
    overrides = {"kernel": kernel_commit, "uboot": uboot_commit}
    locked = {}
    for name, (url, branch) in SOURCES.items():
        commit = overrides.get(name, "")
        if not commit:
            refs = git("ls-remote", "--exit-code", url, f"refs/heads/{branch}").splitlines()
            if len(refs) != 1:
                raise ValueError(f"Ambiguous branch: {url} {branch}")
            commit = refs[0].split()[0]
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError(f"A full lowercase commit hash is required for {name}")
        locked[name] = {"url": url, "branch": branch, "commit": commit}
    manifest = {"builder_commit": git("rev-parse", "HEAD"), "debian_snapshot": snapshot,
                "sources": locked, "toolchain": {"url": TOOLCHAIN_URL, "md5": TOOLCHAIN_MD5}}
    content = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    write_new(STATE / "sources.lock.json", content)
    env = {"R2S_KERNEL_COMMIT": locked["kernel"]["commit"],
           "R2S_UBOOT_COMMIT": locked["uboot"]["commit"], "R2S_DEBIAN_SNAPSHOT": snapshot}
    write_new(STATE / "build.env", "".join(f"{key}={shlex.quote(value)}\n" for key, value in env.items()))
    outputs = {"sources_key": hashlib.sha256(content.encode()).hexdigest(),
               "toolchain_key": TOOLCHAIN_MD5, "builder_commit": manifest["builder_commit"]}
    print(json.dumps(manifest, indent=2))
    if os.environ.get("GITHUB_OUTPUT"):
        # GITHUB_OUTPUT is a runner-managed output channel, not a build cache.
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.writelines(f"{key}={value}\n" for key, value in outputs.items())


def checkout_source(name, source, location):
    mirror = inside(CACHE / "sources" / f"{name}.git")
    mirror.mkdir(parents=True, exist_ok=True)
    if not (mirror / "HEAD").exists():
        git("init", "--bare", str(mirror))
        git("remote", "add", "origin", source["url"], cwd=mirror)
    if git("remote", "get-url", "origin", cwd=mirror) != source["url"]:
        raise ValueError(f"Cached source has an unexpected origin: {name}")
    git("fetch", "--depth=1", "--no-tags", "origin", source["commit"], cwd=mirror)
    git("update-ref", "refs/heads/locked", source["commit"], cwd=mirror)
    git("reflog", "expire", "--expire=now", "--all", cwd=mirror)
    git("gc", "--prune=now", cwd=mirror)
    git("fsck", "--no-dangling", cwd=mirror)
    tree = inside(ROOT / location)
    if tree.exists() and any(tree.iterdir()):
        raise ValueError(f"Refusing to overwrite a source checkout: {tree}")
    tree.mkdir(parents=True, exist_ok=True)
    git("init", str(tree))
    # Copy Git objects, not alternates pointing to a host-specific path.
    git("fetch", "--depth=1", "--no-tags", str(mirror), source["commit"], cwd=tree)
    git("checkout", "--detach", "FETCH_HEAD", cwd=tree)
    if git("rev-parse", "HEAD", cwd=tree) != source["commit"]:
        raise ValueError(f"Source lock mismatch: {name}")
    return tree


def prepare_sources():
    manifest = json.loads((STATE / "sources.lock.json").read_text())
    locations = {
        "kernel": f"kernel/{manifest['sources']['kernel']['commit']}",
        "uboot": f"u-boot/{manifest['sources']['uboot']['commit']}",
        "firmware": "external/cache/sources/orangepi-firmware-git",
        "oh-my-zsh": "external/cache/sources/oh-my-zsh",
        "evalcache": "external/cache/sources/evalcache",
        "wiringOP": "external/cache/sources/wiringOP/next",
        "wiringOP-Python": "external/cache/sources/wiringOP-Python/next",
    }
    submodules = {}
    for name, source in manifest["sources"].items():
        tree = checkout_source(name, source, locations[name])
        if (tree / ".gitmodules").exists():
            # wiringOP-Python embeds wiringOP at a gitlink, not at branch HEAD.
            if name != "wiringOP-Python":
                raise ValueError(f"Submodules need explicit locks before enabling {name}")
            path = git("config", "--file", ".gitmodules", "--get", "submodule.wiringOP.path", cwd=tree)
            url = git("config", "--file", ".gitmodules", "--get", "submodule.wiringOP.url", cwd=tree)
            paths = git("config", "--file", ".gitmodules", "--name-only", "--get-regexp",
                        r"^submodule\..*\.path$", cwd=tree).splitlines()
            if (paths != ["submodule.wiringOP.path"] or path != "wiringOP"
                    or url.removesuffix(".git") != SOURCES["wiringOP"][0].removesuffix(".git")):
                raise ValueError("Unexpected wiringOP-Python submodule")
            entry = git("ls-tree", "HEAD", "wiringOP", cwd=tree).split()
            if len(entry) != 4 or entry[:2] != ["160000", "commit"]:
                raise ValueError("Missing wiringOP gitlink")
            nested = {"url": SOURCES["wiringOP"][0], "commit": entry[2]}
            nested_tree = checkout_source("wiringOP-Python-wiringOP", nested, f"{locations[name]}/wiringOP")
            if (nested_tree / ".gitmodules").exists():
                raise ValueError("Unexpected nested wiringOP submodules")
            submodules["wiringOP-Python/wiringOP"] = nested
    write_new(STATE / "submodules.lock.json", json.dumps(submodules, indent=2) + "\n")
    write_new(STATE / "sources.ready", "Pinned source checkouts and cache verified.\n")


def prepare_toolchain():
    directory = inside(CACHE / "toolchain")
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / f"{TOOLCHAIN}.tar.xz"
    if not archive.exists():
        partial = inside(directory / f"{TOOLCHAIN}.part")
        with urllib.request.urlopen(TOOLCHAIN_URL, timeout=120) as response, partial.open("wb") as dest:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                dest.write(chunk)
        partial.rename(archive)
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            md5.update(chunk)
            sha256.update(chunk)
    if md5.hexdigest() != TOOLCHAIN_MD5:
        raise ValueError("Toolchain does not match the checksum versioned by Orange Pi")
    # The vendor only publishes MD5. Also record SHA-256 before compilation.
    write_new(STATE / "toolchain.lock.json", json.dumps({"url": TOOLCHAIN_URL,
              "md5": md5.hexdigest(), "sha256": sha256.hexdigest()}, indent=2) + "\n")
    write_new(STATE / "toolchain.ready", "Toolchain archive verified.\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["freeze", "sources", "toolchain"])
    parser.add_argument("--snapshot", default="20260928T000000Z")
    parser.add_argument("--kernel-commit", default="")
    parser.add_argument("--uboot-commit", default="")
    args = parser.parse_args()
    if args.stage == "freeze":
        freeze(args.snapshot, args.kernel_commit, args.uboot_commit)
    elif args.stage == "sources":
        prepare_sources()
    else:
        prepare_toolchain()


if __name__ == "__main__":
    main()
