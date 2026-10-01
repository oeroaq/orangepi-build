#!/usr/bin/python3
"""Verified removable-media -> eMMC installer; never selects a disk implicitly."""
import argparse
import ctypes
import datetime
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

import model
from runtime import command, data_mounted


MIB = 1024 * 1024
BOOT_FILES = {"FSBL.bin": (256, 512), "u-boot-env-default.bin": (768, 128),
              "u-boot-opensbi.itb": (1664, 6144)}
CONFIG_PATHS = ("etc/r2s", "etc/NetworkManager/system-connections", "etc/wireguard",
                "etc/ipsec.conf", "etc/ipsec.secrets", "etc/ipsec.d", "etc/swanctl",
                "etc/apt/sources.list.d", "etc/apt/keyrings", "etc/apt/auth.conf.d",
                "etc/ssh", "etc/docker", "etc/containerd", "home/admin/.ssh", "var/lib/r2s/ssh-provisioned")


def digest(path, limit=None, offset=0):
    result = hashlib.sha256()
    with open(path, "rb", buffering=0) as stream:
        stream.seek(offset)
        remaining = limit
        while remaining is None or remaining:
            data = stream.read(min(1024 * 1024, remaining) if remaining is not None else 1024 * 1024)
            if not data:
                if remaining:
                    raise ValueError("Short read while verifying backup/device")
                break
            result.update(data)
            if remaining is not None:
                remaining -= len(data)
    return result.hexdigest()


def copy_bytes(source, target, size, source_offset=0, target_offset=0):
    with open(source, "rb", buffering=0) as src, open(target, "r+b" if Path(target).exists() else "wb", buffering=0) as dst:
        src.seek(source_offset)
        dst.seek(target_offset)
        while size:
            block = src.read(min(MIB, size))
            if not block:
                raise ValueError("Short input while copying a verified partition")
            dst.write(block)
            size -= len(block)
        os.fsync(dst.fileno())


def exchange_roots(left, right):
    """Atomic Btrfs subvolume directory exchange: no missing @root crash window."""
    if left.parent.resolve() != right.parent.resolve() or left.is_symlink() or right.is_symlink():
        raise ValueError("Root exchange requires adjacent real subvolumes")
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.renameat2
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if function(-100, os.fsencode(left), -100, os.fsencode(right), 2):
        raise OSError(ctypes.get_errno(), "Atomic root subvolume exchange failed")


def mounted_target(device):
    records = json.loads(command("lsblk", "--json", "--output", "PATH,MOUNTPOINTS", device))["blockdevices"]
    def visit(items):
        return any(any(record.get("mountpoints") or []) or visit(record.get("children", [])) for record in items)
    return visit(records)


def install(args):
    compatible = Path("/proc/device-tree/compatible").read_bytes()
    if b"orangepi-r2s" not in compatible or os.geteuid() != 0:
        raise ValueError("Installer requires root on Orange Pi R2S")
    target = Path(args.device).resolve()
    if not re.fullmatch(r"/dev/mmcblk[0-9]+", str(target)) or not Path(str(target) + "boot0").is_block_device():
        raise ValueError("An explicit eMMC disk with boot0 is required")
    if not target.is_block_device() or mounted_target(str(target)):
        raise ValueError("Destination eMMC or one of its partitions is mounted")
    rootsource = command("findmnt", "-n", "-o", "SOURCE", "/").split("[", 1)[0]
    parent = command("lsblk", "-n", "-o", "PKNAME", rootsource)
    boot_disk = "/dev/" + parent
    if Path(boot_disk).resolve() == target:
        raise ValueError("Refusing to overwrite the boot disk")
    removable = command("lsblk", "-d", "-n", "-o", "RM", boot_disk) == "1"
    transport = command("lsblk", "-d", "-n", "-o", "TRAN", boot_disk)
    if not removable and transport != "usb":
        raise ValueError("Boot from removable SD/USB before installing to eMMC")
    if not data_mounted():
        raise ValueError("/data must be Btrfs for persistent verified backups")
    source = Path(args.image).resolve()
    if not source.is_file() or not re.fullmatch(r"[a-fA-F0-9]{64}", args.sha256):
        raise ValueError("A regular image and its full SHA-256 are required")
    if digest(source) != args.sha256.lower():
        raise ValueError("Input image SHA-256 mismatch")
    layout = json.loads(Path("/usr/share/r2s/layout.json").read_text())
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = Path("/data/.install")
    if directory.resolve() != directory:
        raise ValueError("Backup directory must not resolve through a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    backup = directory / stamp
    backup.mkdir(mode=0o700)
    raw = backup / "input.img"
    capacity = int(command("blockdev", "--getsize64", str(target)))
    opener = gzip.open if source.name.endswith(".gz") else open
    with opener(source, "rb") as src, raw.open("xb") as dst:
        total = 0
        while True:
            data = src.read(MIB)
            if not data:
                break
            total += len(data)
            if total > capacity:
                raise ValueError("Image does not fit this eMMC")
            dst.write(data)
        dst.flush()
        os.fsync(dst.fileno())
    model.validate_layout(json.loads(command("sfdisk", "--json", str(raw))), layout)
    original_table = command("sfdisk", "--json", str(target))
    (backup / "partition-table.json").write_text(original_table + "\n")
    copy_bytes(target, backup / "header.bin", 30 * MIB)
    copy_bytes(target, backup / "partition-tail.bin", 34 * 512, source_offset=capacity - 34 * 512)
    boot0 = Path(str(target) + "boot0")
    boot0_size = int(command("blockdev", "--getsize64", str(boot0)))
    copy_bytes(boot0, backup / "boot0.bin", boot0_size)
    for name, device, size, offset in (("header.bin", target, 30 * MIB, 0),
            ("partition-tail.bin", target, 34 * 512, capacity - 34 * 512), ("boot0.bin", boot0, boot0_size, 0)):
        if digest(backup / name) != digest(device, size, offset):
            raise ValueError("Backup verification failed before writes")
    record = {"target": str(target), "input_sha256": args.sha256.lower(), "mode": "preserve" if args.preserve_data else "fresh",
              "backups": {name: digest(backup / name) for name in ("header.bin", "partition-tail.bin", "boot0.bin")}}
    (backup / "manifest.json").write_text(json.dumps(record, indent=2) + "\n")
    command("sync")
    work = Path("/run/r2s") / ("install-" + stamp)
    work.mkdir(mode=0o700)
    source_root, source_boot = work / "source-root", work / "source-boot"
    top, destination_boot = work / "target-top", work / "target-boot"
    for path in (source_root, source_boot, top, destination_boot):
        path.mkdir()
    loop = ""
    mounts = []
    changed = False
    exchanged = False
    staged = None
    try:
        # A private reflink and a distinct Btrfs FSID avoid Linux associating a
        # cloned source image with an existing target having the same UUID.
        scratch = backup / "mount-copy.img"
        command("cp", "--reflink=auto", str(raw), str(scratch))
        loop = command("losetup", "--find", "--show", "--partscan", str(scratch))
        command("btrfstune", "-u", loop + "p2")
        command("mount", "-o", "ro,subvol=@root", loop + "p2", str(source_root))
        mounts.append(source_root)
        command("mount", "-o", "ro,noload", loop + "p1", str(source_boot))
        mounts.append(source_boot)
        if json.loads((source_root / "usr/share/r2s/layout.json").read_text()) != layout:
            raise ValueError("Image has an incompatible R2S storage layout")
        binary_dirs = list((source_root / "usr/lib").glob("linux-u-boot-current-orangepir2s_*/"))
        if len(binary_dirs) != 1:
            raise ValueError("Image must include exactly one matching U-Boot build")
        binaries = {}
        for name in (*BOOT_FILES, "bootinfo_emmc.bin"):
            path = binary_dirs[0] / name
            if source_root not in path.resolve().parents or not path.is_file():
                raise ValueError("Invalid bootloader path in image")
            binaries[name] = path.read_bytes()
        for name, (_, sectors) in BOOT_FILES.items():
            if not 0 < len(binaries[name]) <= sectors * 512:
                raise ValueError("Bootloader component exceeds its reserved area")
        if len(binaries["bootinfo_emmc.bin"]) > 512:
            raise ValueError("eMMC bootinfo overlaps FSBL")
        if args.preserve_data:
            model.validate_layout(json.loads(original_table), layout)
            command("mount", "-o", "subvolid=5", str(target) + "p2", str(top))
            mounts.append(top)
            for name in layout["subvolumes"]:
                command("btrfs", "subvolume", "show", str(top / name))
            command("mount", str(target) + "p1", str(destination_boot))
            mounts.append(destination_boot)
            command("rsync", "-aHAX", str(destination_boot) + "/", str(backup / "boot-files") + "/")
            if command("rsync", "-aHAXnci", "--delete", str(destination_boot) + "/", str(backup / "boot-files") + "/"):
                raise ValueError("Boot filesystem backup did not verify")
            staged = top / ("@root.new-" + stamp)
            command("btrfs", "subvolume", "create", str(staged))
            command("rsync", "-aHAX", "--exclude=/boot/*", "--exclude=/data/*", "--exclude=/var/log/*",
                    "--exclude=/.snapshots/*", "--exclude=/var/lib/docker/*", "--exclude=/var/lib/containerd/*",
                    str(source_root) + "/", str(staged) + "/")
            old_root = top / "@root"
            for name in CONFIG_PATHS:
                path = old_root / name
                if path.exists() and old_root in path.resolve().parents:
                    (staged / name).parent.mkdir(parents=True, exist_ok=True)
                    command("rsync", "-aHAX", str(path) + ("/" if path.is_dir() else ""), str(staged / name) + ("/" if path.is_dir() else ""))
            root_uuid = command("blkid", "-s", "UUID", "-o", "value", str(target) + "p2")
            boot_uuid = command("blkid", "-s", "UUID", "-o", "value", str(target) + "p1")
            old_source_root_uuid = re.search(r"UUID=([a-f0-9-]+) / btrfs", (staged / "etc/fstab").read_text()).group(1)
            fstab = (staged / "etc/fstab").read_text().replace(old_source_root_uuid, root_uuid)
            fstab = re.sub(r"UUID=[a-f0-9-]+ /boot ext4", f"UUID={boot_uuid} /boot ext4", fstab)
            (staged / "etc/fstab").write_text(fstab)
            # Capture extra Debian packages without restoring obsolete kernels,
            # foreign OpenWrt metadata or Adblock.
            baseline = set((old_root / "usr/share/r2s/base-packages").read_text().splitlines())
            status = (old_root / "var/lib/dpkg/status").read_text()
            extras = []
            for paragraph in status.split("\n\n"):
                if "Status: install ok installed" not in paragraph:
                    continue
                match = re.search(r"^Package: (.+)$", paragraph, re.M)
                if match and match[1] not in baseline and not match[1].startswith(("linux-", "u-boot", "orangepi-", "r2s-", "adblock", "luci-")):
                    extras.append(match[1])
            upgrade = top / "@data/.upgrade"
            upgrade.mkdir(mode=0o700, exist_ok=True)
            if (upgrade / "pending").exists():
                raise ValueError("An earlier preserved upgrade still needs package restoration")
            (upgrade / "packages").write_text("\n".join(sorted(extras)) + "\n")
            (upgrade / "pending").write_text(stamp + "\n")
            command("btrfs", "subvolume", "snapshot", "-r", str(old_root), str(top / "@snapshots" / ("before-install-" + stamp)))
            changed = True
            command("rsync", "-aHAX", "--delete", str(source_boot) + "/", str(destination_boot) + "/")
            env = destination_boot / "orangepiEnv.txt"
            env.write_text(re.sub(r"^rootdev=.*$", "rootdev=UUID=" + root_uuid, env.read_text(), flags=re.M))
            exchange_roots(old_root, staged)
            exchanged = True
            root_id = command("btrfs", "subvolume", "show", str(old_root))
            command("btrfs", "subvolume", "set-default", re.search(r"Subvolume ID:\s*(\d+)", root_id)[1], str(top))
        else:
            # Give the installed instance unique filesystem/partition UUIDs.
            # This also prevents the live USB and eMMC matching the same root UUID.
            command("mount", "-o", "remount,rw", str(source_root))
            new_root_uuid = command("blkid", "-s", "UUID", "-o", "value", loop + "p2")
            old_fstab = (source_root / "etc/fstab").read_text()
            original_root_uuid = re.search(r"UUID=([a-f0-9-]+) / btrfs", old_fstab)[1]
            command("umount", str(source_boot))
            mounts.remove(source_boot)
            new_boot_uuid = str(uuid.uuid4())
            command("tune2fs", "-U", new_boot_uuid, loop + "p1")
            command("mount", loop + "p1", str(source_boot))
            mounts.append(source_boot)
            fstab = old_fstab.replace(original_root_uuid, new_root_uuid)
            fstab = re.sub(r"UUID=[a-f0-9-]+ /boot ext4", f"UUID={new_boot_uuid} /boot ext4", fstab)
            (source_root / "etc/fstab").write_text(fstab)
            env = source_boot / "orangepiEnv.txt"
            env.write_text(re.sub(r"^rootdev=.*$", "rootdev=UUID=" + new_root_uuid, env.read_text(), flags=re.M))
            command("sfdisk", "--disk-id", loop, "0x" + uuid.uuid4().hex[:8])
            # Unmount source clones before touching or mounting the fresh target.
            for path in reversed(mounts):
                command("umount", str(path))
            mounts.clear()
            command("losetup", "-d", loop)
            loop = ""
            changed = True
            instance_hash = digest(scratch)
            record["installed_sha256"] = instance_hash
            record["root_uuid"] = new_root_uuid
            record["boot_uuid"] = new_boot_uuid
            (backup / "manifest.json").write_text(json.dumps(record, indent=2) + "\n")
            # Explicit fresh-data mode also clears stale primary/backup GPT
            # signatures; otherwise an old GPT tail can confuse growpart.
            command("wipefs", "--all", "--force", str(target))
            copy_bytes(scratch, target, total)
            if digest(target, total) != instance_hash:
                raise ValueError("Written eMMC image does not match the verified source")
        # Only declared p1/p2/boot regions are used. Preserve mode never writes
        # the partition table or Btrfs data region as a raw firmware image.
        for name, (sector, _) in BOOT_FILES.items():
            with target.open("r+b", buffering=0) as stream:
                stream.seek(sector * 512)
                stream.write(binaries[name])
                os.fsync(stream.fileno())
            expected = hashlib.sha256(binaries[name]).hexdigest()
            if digest(target, len(binaries[name]), sector * 512) != expected:
                raise ValueError("Boot partition hash mismatch: " + name)
        if args.update_boot0:
            force_ro = Path("/sys/block") / (target.name + "boot0/force_ro")
            try:
                force_ro.write_text("0\n")
                with boot0.open("r+b", buffering=0) as stream:
                    stream.write(binaries["bootinfo_emmc.bin"])
                    stream.seek(512)
                    stream.write(binaries["FSBL.bin"])
                    os.fsync(stream.fileno())
                if digest(boot0, len(binaries["FSBL.bin"]), 512) != hashlib.sha256(binaries["FSBL.bin"]).hexdigest():
                    raise ValueError("boot0 FSBL verification failed")
            finally:
                force_ro.write_text("1\n")
        command("sync")
        if args.fresh_data:
            command("partprobe", str(target))
            command("partx", "--update", str(target), check=False)
            command("mount", str(target) + "p1", str(destination_boot))
            mounts.append(destination_boot)
            public_key = Path("/home/admin/.ssh/authorized_keys")
            if public_key.is_file() and not public_key.is_symlink():
                firstboot = destination_boot / "r2s-firstboot"
                firstboot.mkdir(exist_ok=True)
                shutil.copyfile(public_key, firstboot / "authorized_keys")
                os.chmod(firstboot / "authorized_keys", 0o600)
            command("sync")
        (backup / "success").write_text("Installation verified.\n")
        print("Installation complete. Backups: " + str(backup))
        print("Add the administrator public key to the target /boot/r2s-firstboot/authorized_keys before booting a fresh image.")
    except Exception:
        if args.preserve_data and changed:
            if exchanged:
                exchange_roots(top / "@root", staged)
                root_id = command("btrfs", "subvolume", "show", str(top / "@root"))
                command("btrfs", "subvolume", "set-default", re.search(r"Subvolume ID:\s*(\d+)", root_id)[1], str(top))
            if (backup / "boot-files").exists():
                command("rsync", "-aHAX", "--delete", str(backup / "boot-files") + "/", str(destination_boot) + "/")
            copy_bytes(backup / "header.bin", target, 30 * MIB)
            force_ro = Path("/sys/block") / (target.name + "boot0/force_ro")
            try:
                force_ro.write_text("0\n")
                copy_bytes(backup / "boot0.bin", boot0, boot0_size)
            finally:
                force_ro.write_text("1\n")
            command("sync")
            pending = top / "@data/.upgrade/pending"
            if pending.exists() and pending.read_text().strip() == stamp:
                pending.unlink()
        (backup / "failed").write_text("See console output; verified boot backups retained.\n")
        raise
    finally:
        for path in reversed(mounts):
            command("umount", str(path), check=False)
        if loop:
            command("losetup", "-d", loop, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--sha256", required=True)
    boot = parser.add_mutually_exclusive_group(required=True)
    boot.add_argument("--reuse-boot0", action="store_true")
    boot.add_argument("--update-boot0", action="store_true")
    data = parser.add_mutually_exclusive_group(required=True)
    data.add_argument("--fresh-data", action="store_true")
    data.add_argument("--preserve-data", action="store_true")
    args = parser.parse_args()
    Path("/run/r2s").mkdir(exist_ok=True)
    with Path("/run/r2s/install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        install(args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit("R2S install: " + str(error))
