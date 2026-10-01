#!/usr/bin/python3
"""Native Debian R2S lifecycle. Configuration generation is separate from effects."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import datetime
import socket
import urllib.request

import model


def command(*args, check=True, input=None):
    return subprocess.run(args, check=check, input=input, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE).stdout.strip()


def atomic(path, text, mode=0o644):
    path = Path(path)
    allowed = (Path("/run/r2s"), Path("/etc/r2s"), Path("/etc/NetworkManager/system-connections"),
               Path("/var/lib/r2s"), Path("/home/admin/.ssh"))
    resolved = path.resolve()
    if not any(parent == resolved.parent or parent in resolved.parents for parent in allowed):
        raise ValueError("Refusing a write outside R2S-managed directories")
    if path.is_file() and path.read_text() == text and (path.stat().st_mode & 0o777) == mode:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        stream.write(text)
        os.fchmod(stream.fileno(), mode)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.replace(path)


def load():
    config = model.router(json.loads(Path("/etc/r2s/router.json").read_text()))
    groups = model.policies(json.loads(Path("/etc/r2s/policies.json").read_text()))
    return config, groups


def ports(config):
    devices = []
    wan = config.get("wan_override")
    for path in Path("/sys/class/net").iterdir():
        if not (path / "device").exists() or (path / "type").read_text().strip() != "1":
            continue
        # Real hardware ports only: never enslave Docker/veth/VPN devices.
        devices.append(path.name)
        node = path / "device/of_node"
        if node.exists() and node.resolve().name.endswith("@" + config["wan_dt_address"]):
            if wan and wan != path.name:
                if config.get("wan_override"):
                    continue
                raise ValueError("Ambiguous WAN device-tree identity")
            wan = path.name
    if wan not in devices:
        raise ValueError("Cannot identify WAN ethernet@cac80000; inspect r2sctl status and set wan_override explicitly")
    lans = sorted(set(devices) - {wan} - set(config.get("additional_wan", [])))
    if not lans or not set(config.get("additional_wan", [])).issubset(devices):
        raise ValueError("Invalid Ethernet WAN/LAN mapping")
    return wan, lans


def networking(config, groups):
    for attempt in range(30):
        try:
            wan, lans = ports(config)
            break
        except ValueError:
            if attempt == 29:
                raise
            time.sleep(1)
    files = model.connections(config, wan, lans)
    for name, content in files.items():
        atomic(f"/etc/NetworkManager/system-connections/r2s-{name}.nmconnection", content, 0o600)
    # NetworkManager's no-auto-default prevents a competing automatic Ethernet
    # profile. Explicit r2s-* keyfiles remain persistent and inspectable by nmcli.
    atomic("/run/r2s/ports.json", json.dumps({"wan": wan, "lan": lans}) + "\n")
    command("sysctl", "-w", f"net/ipv6/conf/{wan}/accept_ra=2")
    refresh(config, groups, wan)
    if command("systemctl", "is-active", "NetworkManager.service", check=False) == "active":
        command("nmcli", "connection", "reload")
        for name in ["lan", *[f"lan-{device}" for device in lans], "wan",
                     *[f"wan{index}" for index in range(1, len(config.get("additional_wan", [])) + 1)]]:
            command("nmcli", "connection", "up", "r2s-" + name)


def policy_routes(groups):
    old_file = Path("/var/lib/r2s/policy-ids.json")
    old_ids = json.loads(old_file.read_text()) if old_file.exists() else []
    for number in set(old_ids) | {g["id"] for g in groups}:
        if type(number) is not int or not 1 <= number <= 99:
            raise ValueError("Invalid saved policy id")
        for family in ("-4", "-6"):
            if number not in old_ids and json.loads(command("ip", "-j", family, "route", "show", "table", str(30000 + number), check=False) or "[]"):
                raise ValueError("Reserved PBR table already contains routes not owned by R2S")
            rules = json.loads(command("ip", "-j", family, "rule", "show"))
            for rule in rules:
                if rule.get("priority") == 10000 + number:
                    mark = rule.get("fwmark", "0")
                    mark = int(mark, 0) if isinstance(mark, str) else mark
                    if mark != 0x5200 + number:
                        raise ValueError("Reserved PBR priority conflicts with another administrator's rule")
                    command("ip", family, "rule", "del", "priority", str(10000 + number))
            command("ip", family, "route", "flush", "table", str(30000 + number), check=False)
    for group in groups:
        number, device = group["id"], group["interface"]
        present = (Path("/sys/class/net") / device).exists()
        for version in (4, 6):
            family, table = f"-{version}", str(30000 + number)
            if group.get("vpn_only"):
                command("ip", family, "route", "add", "unreachable", "default", "table", table, "metric", "32760")
            if present:
                args = ["ip", family, "route", "replace", "default", "table", table, "dev", device, "metric", "10"]
                gateway = group.get(f"gateway{version}")
                if gateway:
                    args += ["via", gateway]
                command(*args, check=False)
            command("ip", family, "rule", "add", "priority", str(10000 + number),
                    "fwmark", hex(0x5200 + number), "table", table)
    atomic(old_file, json.dumps([g["id"] for g in groups]) + "\n")


def failover(config, wan):
    """Monitor additional DHCP WANs and rank only NetworkManager's DHCP routes."""
    if not config.get("additional_wan"):
        return
    state = []
    for index, device in enumerate([wan] + config["additional_wan"]):
        addresses = json.loads(command("ip", "-j", "-4", "address", "show", "dev", device))
        local = next((a["local"] for item in addresses for a in item.get("addr_info", []) if a.get("scope") == "global"), None)
        routes = [r for r in json.loads(command("ip", "-j", "-4", "route", "show", "default", "dev", device))
                  if r.get("protocol") == "dhcp" and r.get("gateway")]
        if not local or not routes:
            continue
        alive = False
        for peer in ("1.1.1.1", "9.9.9.9"):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    probe.settimeout(2)
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device.encode() + b"\0")
                    probe.bind((local, 0))
                    probe.connect((peer, 443))
                    alive = True
                    break
            except OSError:
                pass
        desired = (100 if alive else 10000) + index * 100
        selected = routes[0]
        if len(routes) != 1 or selected.get("metric", 0) != desired:
            # Install the replacement before removing old priorities.
            command("ip", "-4", "route", "replace", "default", "via", selected["gateway"], "dev", device,
                    "proto", "dhcp", "src", local, "metric", str(desired))
            for route in routes:
                if route.get("metric", 0) != desired:
                    command("ip", "-4", "route", "del", "default", "via", route["gateway"], "dev", device,
                            "proto", "dhcp", "metric", str(route.get("metric", 0)), check=False)
        state.append({"interface": device, "reachable": alive, "metric": desired})
    atomic("/run/r2s/wan-health.json", json.dumps(state) + "\n")


def refresh(config, groups, wan=None):
    if wan is None:
        wan = json.loads(Path("/run/r2s/ports.json").read_text())["wan"]
    # Reject overlapping DHCP WAN/LAN instead of silently changing the LAN.
    subnet = ipaddress.ip_interface(config["lan_address"]).network
    for device in [wan] + config.get("additional_wan", []):
        for record in json.loads(command("ip", "-j", "-4", "address", "show", "dev", device)):
            for address in record.get("addr_info", []):
                network = ipaddress.ip_network(f"{address['local']}/{address['prefixlen']}", strict=False)
                if network.overlaps(subnet):
                    raise ValueError("WAN DHCP subnet overlaps LAN 10.0.0.0/24")
    existing_text = command("nft", "-j", "list", "table", "inet", model.TABLE, check=False)
    existing = bool(existing_text)
    sets = [record["set"]["name"] for record in json.loads(existing_text).get("nftables", []) if "set" in record] if existing else []
    bridges = ["docker0"] + [p.name for p in Path("/sys/class/net").iterdir()
                             if re.fullmatch(r"br-[0-9a-f]{12}", p.name)]
    firewall = model.firewall(config, wan, groups, existing, sets, bridges)
    atomic("/run/r2s/firewall.nft", firewall)
    # Atomic updates to our table/chains only; never flush Docker or other tables.
    command("nft", "--check", "--file", "/run/r2s/firewall.nft")
    command("nft", "--file", "/run/r2s/firewall.nft")
    policy_routes(groups)
    failover(config, wan)
    for binary in ("iptables", "ip6tables"):
        command(binary, "-w", "-N", "DOCKER-USER", check=False)
        rules = [["-i", config["lan_bridge"], "-j", "ACCEPT"],
                 ["-o", config["lan_bridge"], "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"]]
        for rule in rules:
            result = subprocess.run([binary, "-w", "-C", "DOCKER-USER", *rule], capture_output=True)
            if result.returncode:
                command(binary, "-w", "-A", "DOCKER-USER", *rule)
    Path("/run/r2s/dns-blocklists").mkdir(exist_ok=True)
    if Path("/data/dns/blocklist.conf").exists() and data_mounted():
        atomic("/run/r2s/dns-blocklists/blocklist.conf", Path("/data/dns/blocklist.conf").read_text())
    dns = model.dns_config(config, groups)
    changed = not Path("/run/r2s/dnsmasq.conf").exists() or Path("/run/r2s/dnsmasq.conf").read_text() != dns
    atomic("/run/r2s/dnsmasq.conf", dns)
    command("dnsmasq", "--test", "--conf-file=/run/r2s/dnsmasq.conf")
    if changed:
        command("systemctl", "try-restart", "r2s-dns.service", check=False)


def data_mounted():
    return command("findmnt", "--mountpoint", "/data", "-n", "-o", "FSTYPE", check=False) == "btrfs"


def update_blocklist(config):
    if not data_mounted():
        raise ValueError("/data is not a mounted Btrfs subvolume")
    with urllib.request.urlopen(config["blocklist_url"], timeout=60) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("Blocklist download too large")
    allowed = [line.strip() for line in Path("/etc/r2s/allowlist.txt").read_text().splitlines()
               if line.strip() and not line.startswith("#")]
    rendered = model.blocklist(raw.decode("utf-8"), allowed)
    directory = Path("/data/dns").resolve()
    if directory != Path("/data/dns"):
        raise ValueError("DNS data path must not be a symlink")
    directory.mkdir(mode=0o750, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=directory, delete=False) as stream:
        stream.write(rendered)
        os.fchmod(stream.fileno(), 0o640)
        stream.flush()
        os.fsync(stream.fileno())
        candidate = Path(stream.name)
    try:
        command("dnsmasq", "--test", "--conf-file=" + str(candidate))
        candidate.replace(directory / "blocklist.conf")
        atomic("/run/r2s/dns-blocklists/blocklist.conf", rendered)
        atomic("/var/lib/r2s/blocklist.json", json.dumps({"url": config["blocklist_url"],
               "sha256": hashlib.sha256(raw).hexdigest(), "domains": rendered.count("\n")}) + "\n")
        command("systemctl", "restart", "r2s-dns.service")
    finally:
        if candidate.exists():
            candidate.unlink()


def firstboot():
    command("ssh-keygen", "-A")
    if Path("/var/lib/r2s/ssh-provisioned").exists():
        return
    key_file = Path("/boot/r2s-firstboot/authorized_keys")
    if not key_file.exists():
        print("Add a public key to /boot/r2s-firstboot/authorized_keys, then run r2sctl firstboot.")
        return
    if key_file.is_symlink() or key_file.stat().st_size > 65536:
        raise ValueError("Invalid firstboot public-key file")
    keys = []
    for line in key_file.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if not re.match(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/=]+(?: .*)?$", line):
            raise ValueError("Only plain SSH public keys are accepted; no private keys/options")
        keys.append(line)
    if not keys:
        raise ValueError("No public keys found")
    command("ssh-keygen", "-l", "-f", str(key_file))
    atomic("/home/admin/.ssh/authorized_keys", "\n".join(keys) + "\n", 0o600)
    command("chown", "-R", "admin:admin", "/home/admin/.ssh")
    command("chmod", "700", "/home/admin/.ssh")
    command("sshd", "-t")
    atomic("/var/lib/r2s/ssh-provisioned", "Administrator public key imported.\n", 0o600)


def prepare_metrics():
    if not data_mounted():
        raise ValueError("Metrics require /data to be mounted as Btrfs")
    base = Path("/data/metrics")
    if base.resolve() != base:
        raise ValueError("Metrics path must not be a symlink")
    command("install", "-d", "-m", "0750", "/data/metrics/rrd")
    command("install", "-d", "-m", "0750", "-o", "vnstat", "-g", "vnstat", "/data/metrics/vnstat")


def restore_packages():
    if not data_mounted():
        raise ValueError("Package restoration requires /data to be mounted")
    directory = Path("/data/.upgrade")
    if directory.resolve() != directory:
        raise ValueError("Upgrade state must not resolve through a symlink")
    pending = directory / "pending"
    if not pending.exists():
        return
    packages = (directory / "packages").read_text().splitlines()
    for name in packages:
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", name) or name.startswith(("linux-", "u-boot", "orangepi-", "r2s-", "adblock", "luci-")):
            raise ValueError("Invalid or excluded package in restore manifest")
    with (directory / "restore.log").open("a") as log:
        try:
            subprocess.run(["apt-get", "update"], check=True, stdout=log, stderr=log)
            if packages:
                subprocess.run(["apt-get", "install", "-y", "--no-install-recommends", *packages], check=True,
                               stdout=log, stderr=log, env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
            command("apt-get", "clean")
            pending.rename(directory / "restored")
        except Exception:
            (directory / "failed").write_text("Package restoration failed; inspect restore.log.\n")
            raise


def snapshot():
    if not data_mounted() or command("findmnt", "-n", "-o", "FSTYPE", "/") != "btrfs":
        raise ValueError("Snapshots require the mounted R2S Btrfs layout")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path("/.snapshots") / ("system-" + stamp)
    if command("findmnt", "--mountpoint", "/.snapshots", "-n", "-o", "FSTYPE") != "btrfs":
        raise ValueError("Snapshot subvolume is not mounted")
    backup = Path("/data/.boot-snapshots")
    if backup.resolve() != backup:
        raise ValueError("Boot snapshot path must not be a symlink")
    backup.mkdir(mode=0o700, exist_ok=True)
    boot_archive = backup / (stamp + ".tar.gz")
    command("tar", "-czf", str(boot_archive), "-C", "/boot", ".")
    command("tar", "-tzf", str(boot_archive))
    command("sync")
    command("btrfs", "subvolume", "snapshot", "-r", "/", str(target))
    (backup / (stamp + ".sha256")).write_text(hashlib.sha256(boot_archive.read_bytes()).hexdigest() + "  " + boot_archive.name + "\n")
    print(str(target) + " paired with " + str(boot_archive))


def shaping():
    entries = model.sqm(json.loads(Path("/etc/r2s/sqm.json").read_text()))
    state = Path("/var/lib/r2s/sqm-devices.json")
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    old_state = json.loads(state.read_text()) if state.exists() else {}
    old = old_state.get("devices", [])
    if old_state.get("digest") == digest and all((Path("/sys/class/net") / name).exists() for name in old):
        return
    for name in old:
        model.interface(name)
        command("tc", "qdisc", "del", "dev", name, "root", check=False)
        command("tc", "qdisc", "del", "dev", name, "ingress", check=False)
    active = []
    for number, entry in enumerate(entries):
        if not entry.get("enabled", True):
            continue
        device, ifb = entry["interface"], f"r2sifb{number}"
        command("modprobe", "ifb")
        command("modprobe", "sch_cake")
        command("ip", "link", "add", ifb, "type", "ifb", check=False)
        command("ip", "link", "set", ifb, "up")
        command("tc", "qdisc", "replace", "dev", device, "root", "handle", "1:", "cake",
                "bandwidth", f"{entry['upload_kbit']}kbit", "diffserv4", "nat")
        command("tc", "qdisc", "replace", "dev", device, "handle", "ffff:", "ingress")
        command("tc", "filter", "replace", "dev", device, "parent", "ffff:", "protocol", "all",
                "priority", "10", "matchall", "action", "mirred", "egress", "redirect", "dev", ifb)
        command("tc", "qdisc", "replace", "dev", ifb, "root", "cake", "bandwidth",
                f"{entry['download_kbit']}kbit", "besteffort", "nat", "ingress")
        active += [device, ifb]
    atomic(state, json.dumps({"digest": digest, "devices": active}) + "\n")


def grow():
    layout = json.loads(Path("/usr/share/r2s/layout.json").read_text())
    source = command("findmnt", "-n", "-o", "SOURCE", "/").split("[", 1)[0]
    source = str(Path(source).resolve())
    if not re.fullmatch(r"/dev/(mmcblk[0-9]+p2|sd[a-z]+2|nvme[0-9]+n[0-9]+p2)", source):
        raise ValueError("Unexpected root device; no automatic partition changes")
    parent = command("lsblk", "-n", "-o", "PKNAME", source)
    disk = "/dev/" + parent
    model.validate_layout(json.loads(command("sfdisk", "--json", disk)), layout)
    if command("findmnt", "-n", "-o", "FSTYPE", "/") != "btrfs":
        raise ValueError("Root is not Btrfs")
    backup = Path("/var/lib/r2s/partition-table.before-grow")
    if not backup.exists():
        atomic(backup, command("sfdisk", "--dump", disk) + "\n", 0o600)
    result = subprocess.run(["growpart", disk, "2"], text=True, capture_output=True)
    if result.returncode not in (0, 1) or (result.returncode == 1 and "NOCHANGE" not in result.stdout):
        raise ValueError("growpart failed: " + result.stderr)
    model.validate_layout(json.loads(command("sfdisk", "--json", disk)), layout)
    command("partx", "--update", "--nr", "2", disk, check=False)
    command("btrfs", "filesystem", "resize", "max", "/")
    atomic("/var/lib/r2s/grow.complete", "Shared Btrfs pool expanded.\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["network", "refresh", "firstboot", "update-blocklist", "sqm", "grow", "prepare-metrics", "restore", "snapshot", "status", "check"])
    args = parser.parse_args()
    config, groups = load() if args.action != "firstboot" else (None, None)
    if args.action == "check":
        model.sqm(json.loads(Path("/etc/r2s/sqm.json").read_text()))
        print("R2S configuration valid")
        return
    if args.action == "status":
        print(json.dumps({"config": config, "ports": ports(config), "policies": groups}, indent=2))
        return
    if os.geteuid() != 0:
        raise SystemExit("Run r2sctl with sudo")
    Path("/run/r2s").mkdir(exist_ok=True)
    with Path("/run/r2s/operation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        actions = {"network": lambda: networking(config, groups), "refresh": lambda: refresh(config, groups),
                   "firstboot": firstboot, "update-blocklist": lambda: update_blocklist(config), "sqm": shaping,
                   "grow": grow, "prepare-metrics": prepare_metrics, "restore": restore_packages, "snapshot": snapshot}
        actions[args.action]()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"R2S: {error}")
