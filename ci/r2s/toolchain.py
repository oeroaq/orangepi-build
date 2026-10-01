#!/usr/bin/env python3
"""Extract a verified relocatable KY SDK without links into the builder host."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import tarfile
import tempfile


PREFIX = "ky-toolchain-linux-glibc-x86_64-v1.0.1"
# This installation prefix is present in the checksum-verified vendor SDK.
# It is a relocation marker, never a filesystem path we read or write.
VENDOR_PREFIX = "/home/ci/gcc_linux_install"
ROOT = Path(__file__).resolve().parents[2]


def contained(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != PREFIX:
        raise ValueError(f"Unsafe toolchain member: {name!r}")
    return path.as_posix()


def link_target(entry):
    member = PurePosixPath(contained(entry.name))
    link = PurePosixPath(entry.linkname)
    if link.is_absolute():
        if entry.linkname == VENDOR_PREFIX or entry.linkname.startswith(VENDOR_PREFIX + "/"):
            target = PREFIX + entry.linkname[len(VENDOR_PREFIX):]
        elif (len(member.parts) > 1 and member.parts[1] == "sysroot"
              and link.parts[1:2] in (("lib",), ("lib64",), ("usr",), ("etc",))):
            # Sysroot absolute links mean paths in the guest, not host /lib.
            target = PREFIX + "/sysroot" + entry.linkname
        else:
            raise ValueError(f"Unsafe absolute toolchain link: {entry.name!r} -> {entry.linkname!r}")
    else:
        # Symlinks are relative to their parent. Tar hardlink names are
        # relative to the archive root, not to the link's parent directory.
        base = member.parent if entry.issym() else PurePosixPath(".")
        target = (base / link).as_posix()
    target = posixpath.normpath(target)
    return contained(target)


def validate_members(entries):
    members = {}
    links = {}
    for entry in entries:
        name = contained(entry.name)
        if name in members:
            raise ValueError(f"Duplicate toolchain member: {name}")
        if not (entry.isdir() or entry.isfile() or entry.issym() or entry.islnk()):
            raise ValueError(f"Unsupported toolchain member type: {name}")
        members[name] = entry
        if entry.issym() or entry.islnk():
            try:
                links[name] = link_target(entry)
            except ValueError as error:
                raise ValueError(f"Invalid toolchain link: {name!r} -> {entry.linkname!r}: {error}") from error
    # No data is extracted through a link, even if it appears later in tar.
    for name in members:
        for parent in PurePosixPath(name).parents:
            if parent.as_posix() in links:
                raise ValueError(f"Toolchain member traverses an archive link: {name}")

    def resolve(name, seen=()):
        path = PurePosixPath(name)
        for index in range(1, len(path.parts) + 1):
            ancestor = PurePosixPath(*path.parts[:index]).as_posix()
            if ancestor in links:
                if ancestor in seen or len(seen) >= 40:
                    raise ValueError(f"Cyclic toolchain link: {ancestor}")
                suffix = PurePosixPath(*path.parts[index:])
                target = contained(posixpath.normpath((PurePosixPath(links[ancestor]) / suffix).as_posix()))
                return resolve(target, (*seen, ancestor))
        return name

    for name, target in links.items():
        resolved = resolve(target, (name,))
        if members[name].islnk():
            if resolved not in members or not members[resolved].isfile():
                raise ValueError(f"Toolchain hardlink lacks a regular target: {name} -> {target}")
        # Dangling relative links confined to the SDK are allowed. GCC/binutils
        # may intentionally ship them for optional runtime/sysroot components.
        links[name] = target
    return members, links, resolve


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def extract(archive, destination, expected_sha256):
    archive = Path(archive).resolve()
    destination = Path(destination).resolve()
    if ROOT not in archive.parents or ROOT not in destination.parents:
        raise ValueError("Toolchain archive and destination must be inside the checkout")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise ValueError("A full immutable SHA-256 is required before extraction")
    actual = sha256(archive)
    if actual != expected_sha256:
        raise ValueError(f"Toolchain SHA-256 mismatch: expected {expected_sha256}, got {actual}")
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / PREFIX
    if target.exists() or target.is_symlink():
        raise ValueError(f"Refusing to overwrite an existing toolchain: {target}")
    # Stage all data and links in a fresh directory under the authorized root.
    # Invalid archives cannot partially replace an existing compiler.
    with tempfile.TemporaryDirectory(prefix=".ky-extract-", dir=destination) as temporary:
        stage = Path(temporary)
        with tarfile.open(archive, "r:xz") as source:
            entries = source.getmembers()
            members, links, resolve = validate_members(entries)
            for entry in entries:
                name = contained(entry.name)
                path = stage / name
                if entry.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif entry.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(entry) as data, path.open("xb") as output:
                        shutil.copyfileobj(data, output)
                    os.chmod(path, entry.mode & 0o777)
            for name, target_name in links.items():
                entry, path = members[name], stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if entry.islnk():
                    os.link(stage / resolve(target_name), path)
                else:
                    # Normalize/rebase every link to a relative SDK-local path.
                    relative = posixpath.relpath(target_name, PurePosixPath(name).parent.as_posix())
                    path.symlink_to(relative)
            (stage / PREFIX).rename(target)
    print(f"Verified/extracted KY toolchain: {len(members)} members, {len(links)} SDK-local links")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("--destination", default="toolchains")
    parser.add_argument("--lock", required=True)
    args = parser.parse_args()
    lock = json.loads(Path(args.lock).read_text())
    digest = lock.get("toolchain_sha256", lock.get("sha256"))
    extract(args.archive, args.destination, digest)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, tarfile.TarError) as error:
        raise SystemExit(f"KY toolchain: {error}")
