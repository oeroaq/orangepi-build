#!/usr/bin/env python3
"""Sealed, run-scoped artifacts for the R2S multijob build pipeline."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[2]
ROLES = {"context", "environment", "toolchain", "source-kernel", "source-uboot", "source-firmware",
         "kernel", "uboot", "rootfs", "image", "release"}
LOCK_FILES = ("sources.lock.json", "toolchain.lock.json", "container.lock.json", "build.env", "source-date-epoch")


def safe(path):
    path = Path(path).resolve()
    if ROOT not in path.parents:
        raise ValueError(f"Path outside checkout: {path}")
    return path


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def inventory(directory):
    files = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Artifact payload must not contain symlinks: {path}")
        safe(path)
        if path.is_file() and path.name != "manifest.json":
            files[path.relative_to(directory).as_posix()] = {"sha256": digest(path), "size": path.stat().st_size}
    return files


def directories(directory):
    return sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_dir())


def identity():
    return json.loads((ROOT / "_ci/state/context.json").read_text())


def seal(role, directory, context):
    if role not in ROLES:
        raise ValueError("Unknown artifact role")
    directory = safe(directory)
    files = inventory(directory)
    if not files:
        raise ValueError("Empty artifact payload")
    manifest = {"schema": 2, "role": role, "identity": context, "producer_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local"),
                "files": files, "directories": directories(directory)}
    with (directory / "manifest.json").open("x") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
    output = safe(ROOT / "_ci/artifacts" / f"{role}.tar")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError("Refusing to overwrite a sealed artifact")
    with tarfile.open(output, "w") as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file() or path.is_dir():
                archive.add(path, arcname=path.relative_to(directory).as_posix(), recursive=False)
    return output


def verify(role, directory, expected):
    directory = safe(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("schema") != 2 or manifest.get("role") != role or manifest.get("identity") != expected:
        raise ValueError("Artifact role/build identity does not match this execution")
    if (not manifest.get("files") or inventory(directory) != manifest["files"]
            or directories(directory) != manifest.get("directories")):
        raise ValueError("Artifact inventory/checksums do not match its manifest")
    return manifest


def unpack(role, archive_path, expected=None, build_id=None):
    archive_path = safe(archive_path)
    directory = safe(ROOT / "_ci/inbox" / role)
    if directory.exists():
        raise ValueError("Refusing to overwrite an imported artifact")
    # Validate every member before extraction. Preserve empty directories too:
    # Git requires refs/ even when all refs have moved into packed-refs.
    # rootfs/OCI/toolchain archives remain opaque, independently hashed files.
    with tarfile.open(archive_path, "r:") as archive:
        members = archive.getmembers()
        names = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts or member.name in names
                    or not (member.isfile() or member.isdir()) or member.size < 0 or member.size > 8_000_000_000):
                raise ValueError("Unsafe or duplicate artifact archive member")
            names.add(member.name)
        directory.mkdir(parents=True)
        for member in members:
            path = safe(directory / member.name)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as output, archive.extractfile(member) as source:
                shutil.copyfileobj(source, output)
    if role == "context":
        context = json.loads((directory / "state/context.json").read_text())
        if (context.get("build_id") != build_id or context.get("run_id") != os.environ.get("GITHUB_RUN_ID")
                or context.get("builder_commit") != subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()):
            raise ValueError("Preparation context belongs to another run or builder commit")
        expected = context
    verify(role, directory, expected)
    return directory


def copy_file(source, destination):
    source, destination = safe(source), safe(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError(f"Refusing to overwrite transferred file: {destination}")
    shutil.copyfile(source, destination)


def copy_tree(source, destination):
    source, destination = safe(source), safe(destination)
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("Unexpected symlink in an artifact transfer")
        if path.is_dir():
            safe(destination / path.relative_to(source)).mkdir(parents=True, exist_ok=True)
        elif path.is_file() and path.name != "manifest.json":
            copy_file(path, destination / path.relative_to(source))


def prepare(build_id):
    if not re.fullmatch(r"r2s-\d+-\d+", build_id):
        raise ValueError("Invalid run-scoped build id")
    state = ROOT / "_ci/state"
    sources = json.loads((state / "sources.lock.json").read_text())
    container = json.loads((state / "container.lock.json").read_text())[0]
    epoch = subprocess.check_output(["git", "-C", str(ROOT / "kernel" / sources["sources"]["kernel"]["commit"]),
                                     "show", "-s", "--format=%ct", "HEAD"], text=True).strip()
    with (state / "source-date-epoch").open("x") as stream:
        stream.write(epoch + "\n")
    context = {"build_id": build_id, "run_id": os.environ["GITHUB_RUN_ID"],
               "builder_commit": sources["builder_commit"], "architecture": "riscv64",
               "sources_sha256": digest(state / "sources.lock.json"), "sources": sources["sources"],
               "debian_snapshot": sources["debian_snapshot"], "source_date_epoch": int(epoch),
               "container_id": container["Id"], "toolchain_sha256": json.loads((state / "toolchain.lock.json").read_text())["sha256"]}
    with (state / "context.json").open("x") as stream:
        json.dump(context, stream, indent=2, sort_keys=True)
    directory = ROOT / "_ci/payload/context/state"
    directory.mkdir(parents=True)
    for name in (*LOCK_FILES, "context.json"):
        copy_file(state / name, directory / name)
    seal("context", directory.parent, context)
    for component in ("kernel", "uboot", "firmware"):
        payload = ROOT / f"_ci/payload/source-{component}"
        copy_tree(ROOT / f"_ci/cache/sources/{component}.git", payload / f"{component}.git")
        seal("source-" + component, payload, context)
    payload = ROOT / "_ci/payload/toolchain"
    archives = list((ROOT / "_ci/cache/toolchain").glob("*.tar.xz"))
    if len(archives) != 1 or digest(archives[0]) != context["toolchain_sha256"]:
        raise ValueError("Toolchain hash changed after preparation")
    copy_file(archives[0], payload / archives[0].name)
    seal("toolchain", payload, context)
    environment = ROOT / "_ci/payload/environment"
    environment.mkdir(parents=True)
    # Exact OCI image, not a second rebuild of APT layers in each job.
    with (environment / "builder.tar.zst").open("xb") as output:
        export = subprocess.Popen(["docker", "save", context["container_id"]], stdout=subprocess.PIPE)
        compress = subprocess.run(["zstd", "-T0", "-3"], stdin=export.stdout, stdout=output, check=True)
        export.stdout.close()
        if export.wait() != 0 or compress.returncode:
            raise ValueError("Failed to export the shared builder")
    seal("environment", environment, context)
    compat = hashlib.sha256((context["container_id"] + context["toolchain_sha256"]).encode()).hexdigest()
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write(f"namespace={build_id}\ncompat={compat}\nrootfs={context['builder_commit']}-{context['debian_snapshot']}-{compat}\n")


def import_artifact(role, build_id=None):
    expected = None if role == "context" else identity()
    directory = unpack(role, ROOT / f"_ci/artifacts/{role}.tar", expected, build_id)
    if role == "context":
        copy_tree(directory / "state", ROOT / "_ci/state")
    elif role == "environment":
        # Loading occurs in the shell helper, after inventory validation.
        pass
    elif role == "toolchain":
        copy_tree(directory, ROOT / "_ci/cache/toolchain")
    elif role.startswith("source-"):
        component = role.removeprefix("source-")
        copy_tree(directory / f"{component}.git", ROOT / f"_ci/cache/sources/{component}.git")
    elif role in ("kernel", "uboot", "rootfs"):
        copy_tree(directory / "debs", ROOT / "output/debs")
        if role == "rootfs":
            copy_tree(directory / "rootfs", ROOT / "external/cache/rootfs")
        else:
            copy_tree(directory / "references", ROOT / f"_ci/references/{role}")
        copy_tree(directory / "logs", ROOT / f"_ci/logs/{role}")
    elif role == "image":
        copy_tree(directory / "images", ROOT / "_ci/image")
        copy_tree(directory / "debs", ROOT / "output/debs")
        copy_tree(directory / "logs", ROOT / "_ci/logs/assemble")
    elif role == "release":
        copy_tree(directory / "release", ROOT / "_ci/release")
    return directory


def component_sources(component):
    context = identity()
    source = context["sources"][component]
    mirror = ROOT / f"_ci/cache/sources/{component}.git"
    if subprocess.check_output(["git", "--git-dir", str(mirror), "rev-parse", "refs/heads/locked"], text=True).strip() != source["commit"]:
        raise ValueError("Source bundle does not match its locked commit")
    tree = ROOT / ({"kernel": "kernel", "uboot": "u-boot"}[component] + "/" + source["commit"]) if component != "firmware" else ROOT / "external/cache/sources/orangepi-firmware-git"
    if tree.exists():
        raise ValueError("Refusing to overwrite a component checkout")
    tree.mkdir(parents=True)
    subprocess.run(["git", "init", str(tree)], check=True)
    subprocess.run(["git", "-C", str(tree), "fetch", "--depth=1", str(mirror), source["commit"]], check=True)
    subprocess.run(["git", "-C", str(tree), "checkout", "--detach", "FETCH_HEAD"], check=True)


def capture(role):
    context = identity()
    payload = ROOT / f"_ci/payload/{role}"
    payload.mkdir(parents=True, exist_ok=False)
    if role in ("kernel", "uboot", "rootfs"):
        debs = list((ROOT / "output/debs").rglob("*.deb"))
        if not debs:
            raise ValueError("Build did not produce component packages")
        for path in debs:
            copy_file(path, payload / "debs" / path.relative_to(ROOT / "output/debs"))
        copy_tree(ROOT / "_ci/logs", payload / "logs")
        if (ROOT / "output/debug").exists():
            copy_tree(ROOT / "output/debug", payload / "logs/builder")
        if role == "kernel":
            tree = ROOT / "kernel" / context["sources"]["kernel"]["commit"]
            files = {".config": "kernel.config", "Makefile": "Makefile", "Module.symvers": "Module.symvers",
                     "include/config/kernel.release": "kernel.release", "arch/riscv/boot/Image": "Image",
                     "arch/riscv/boot/dts/ky/x1_orangepi-r2s.dtb": "x1_orangepi-r2s.dtb"}
            for source, destination in files.items():
                copy_file(tree / source, payload / "references" / destination)
        elif role == "uboot":
            tree = ROOT / "u-boot" / context["sources"]["uboot"]["commit"]
            for name in ("bootinfo_sd.bin", "bootinfo_emmc.bin", "FSBL.bin", "u-boot-env-default.bin", "u-boot-opensbi.itb", ".config"):
                copy_file(tree / name, payload / "references" / name)
        else:
            copy_tree(ROOT / "external/cache/rootfs", payload / "rootfs")
    elif role == "image":
        images = list((ROOT / "output/images").glob("*/*.img.gz"))
        if len(images) != 1:
            raise ValueError("Expected exactly one assembled image")
        copy_file(images[0], payload / "images" / images[0].name)
        copy_file(Path(str(images[0]) + ".sha"), payload / "images" / (images[0].name + ".sha"))
        copy_file(ROOT / "_ci/state/raw-image.sha256", payload / "images/raw-image.sha256")
        platform = list((ROOT / "output/debs").glob("r2s-platform_*_all.deb"))
        if len(platform) != 1:
            raise ValueError("Assembly must produce exactly one native platform package")
        copy_file(platform[0], payload / "debs" / platform[0].name)
        copy_tree(ROOT / "_ci/logs", payload / "logs")
        copy_tree(ROOT / "output/debug", payload / "logs/builder")
    elif role == "release":
        copy_tree(ROOT / "_ci/release", payload / "release")
    else:
        raise ValueError("Cannot capture this role")
    seal(role, payload, context)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "import", "sources", "capture", "check-environment", "check-components"])
    parser.add_argument("value", nargs="?")
    parser.add_argument("--build-id")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.build_id)
    elif args.action == "import":
        import_artifact(args.value, args.build_id)
    elif args.action == "sources":
        component_sources(args.value)
    elif args.action == "capture":
        capture(args.value)
    elif args.action == "check-environment":
        actual = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", identity()["container_id"]], text=True).strip()
        if actual != identity()["container_id"]:
            raise ValueError("Loaded builder differs from the prepared image")
    else:
        for role in ("kernel", "uboot", "rootfs"):
            verify(role, ROOT / "_ci/inbox" / role, identity())


if __name__ == "__main__":
    main()
