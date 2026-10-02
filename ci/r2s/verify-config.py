#!/usr/bin/env python3
"""Validate the actual image's mount/configuration contract, not just templates."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "package/r2s-platform/root/usr/lib/r2s"))
import model


def verify(root, top, boot_uuid, root_uuid):
    layout = json.loads((root / "usr/share/r2s/layout.json").read_text())
    model.router(json.loads((root / "etc/r2s/router.json").read_text()))
    model.policies(json.loads((root / "etc/r2s/policies.json").read_text()))
    model.sqm(json.loads((root / "etc/r2s/sqm.json").read_text()))
    entries = [line.split() for line in (root / "etc/fstab").read_text().splitlines() if line and not line.startswith("#")]
    for volume, mount in layout["subvolumes"].items():
        entry = [row for row in entries if row[1] == mount]
        if len(entry) != 1 or entry[0][0] != "UUID=" + root_uuid or entry[0][2] != "btrfs":
            raise ValueError("Incorrect Btrfs mount entry: " + mount)
        options = entry[0][3].split(",")
        if "subvol=" + volume not in options or "compress=zstd:3" not in options or "noatime" not in options:
            raise ValueError("Missing persistent Btrfs mount options: " + mount)
        if not (top / volume).is_dir():
            raise ValueError("Missing subvolume: " + volume)
    boot = [row for row in entries if row[1] == "/boot"]
    if len(boot) != 1 or boot[0][0] != "UUID=" + boot_uuid or boot[0][2] != "ext4":
        raise ValueError("Incorrect boot mount")
    env = {}
    for line in (root / "boot/orangepiEnv.txt").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key in env:
            raise ValueError("Duplicate boot environment option: " + key)
        env[key] = value
    args = env.get("extraargs", "").split()
    if env.get("rootdev") != "UUID=" + root_uuid or env.get("rootfstype") != "btrfs" or "rootflags=subvol=@root" not in args:
        raise ValueError("Boot environment does not match the Btrfs root")
    for key, expected in (("verbosity", "7"), ("console", "serial"), ("earlycon", "on")):
        if env.get(key) != expected:
            raise ValueError("Missing serial boot diagnostics: " + key)
    if not {"mem=2G", "ignore_loglevel"}.issubset(args) or "keep_bootcon" in args:
        raise ValueError("Incorrect 2 GiB memory limit or serial console handoff")
    if [arg for arg in args if arg.startswith("init=")] != ["init=/bin/sh"]:
        raise ValueError("Diagnostic image must hand off to /bin/sh instead of systemd")
    if [arg for arg in args if arg.startswith("panic=")] != ["panic=0"]:
        raise ValueError("Diagnostic image must not automatically reboot on a kernel panic")
    if (root / "etc/docker/daemon.json").exists():
        raise ValueError("Router profile must keep the Docker daemon's defaults")
    units = (root / "usr/lib/systemd/system").glob("r2s-*.service")
    for unit in units:
        name = unit.name
        if not (root / "etc/systemd/system/multi-user.target.wants" / name).is_symlink() and name not in ("r2s-blocklist.service", "r2s-refresh.service"):
            raise ValueError("Required R2S unit is not enabled: " + name)


if __name__ == "__main__":
    root, top = map(Path, sys.argv[1:3])
    uuids = [subprocess.check_output(["blkid", "-s", "UUID", "-o", "value", part], text=True).strip() for part in sys.argv[3:5]]
    verify(root, top, *uuids)
