"""Validated, side-effect-free R2S configuration used by build and runtime tests."""
import base64
import ipaddress
import json
import re
import struct
import uuid


TABLE = "r2s_router"
# Command-form nft syntax accepts unquoted identifiers, not quoted strings.
# Prefix every logical name so lexer keywords such as mark/masquerade never
# become identifiers. Keep hook/statement keywords unchanged.
CHAINS = {name: "r2s_" + name for name in ("input", "forward", "output", "mark", "dns_redirect", "masquerade")}
INTERFACE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}\Z")
DOMAIN = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}\Z")


def interface(value):
    if not isinstance(value, str) or not INTERFACE.fullmatch(value):
        raise ValueError("Invalid interface name")
    return value


def router(value):
    value = dict(value)
    interface(value["lan_bridge"])
    address = ipaddress.ip_interface(value["lan_address"])
    if address.version != 4 or address.network.prefixlen > 29:
        raise ValueError("LAN must be an IPv4 subnet with room for DHCP clients")
    start, end = (ipaddress.ip_address(value[k]) for k in ("dhcp_start", "dhcp_end"))
    if not (start in address.network and end in address.network and int(start) <= int(end)):
        raise ValueError("DHCP range is outside LAN")
    if int(start) <= int(address.ip) <= int(end):
        raise ValueError("DHCP range includes the router")
    if start == address.network.network_address or end == address.network.broadcast_address:
        raise ValueError("DHCP range includes a reserved subnet address")
    if not re.fullmatch(r"[1-9][0-9]{0,3}[mh]", value["lease_time"]):
        raise ValueError("Invalid lease time")
    if not DOMAIN.fullmatch(value["domain"]):
        raise ValueError("Invalid LAN domain")
    if not re.fullmatch(r"[a-f0-9]{8}", value["wan_dt_address"]):
        raise ValueError("Invalid WAN device-tree address")
    if value.get("wan_override"):
        interface(value["wan_override"])
    additional = value.get("additional_wan", [])
    if len(additional) != len(set(additional)):
        raise ValueError("Repeated WAN interface")
    for name in additional:
        interface(name)
    prefix = value.get("ipv6_lan_prefix")
    if prefix:
        network = ipaddress.ip_network(prefix, strict=True)
        if (network.version != 6 or network.prefixlen != 64 or network.is_link_local
                or network.network_address.is_multicast or network.network_address.is_unspecified):
            raise ValueError("IPv6 LAN needs an explicitly routed /64")
    if not value["blocklist_url"].startswith("https://") or any(c in value["blocklist_url"] for c in "\r\n"):
        raise ValueError("Blocklist must use HTTPS")
    return value


def policies(value):
    groups = value.get("groups", [])
    ids = set()
    for group in groups:
        number = group["id"]
        if type(number) is not int or not 1 <= number <= 99 or number in ids:
            raise ValueError("Policy ids must be unique integers in 1..99")
        ids.add(number)
        interface(group["interface"])
        if not isinstance(group.get("vpn_only", False), bool):
            raise ValueError("vpn_only must be boolean")
        for source in group.get("sources", []):
            ipaddress.ip_network(source, strict=True)
        for domain in group.get("domains", []):
            if not DOMAIN.fullmatch(domain):
                raise ValueError("Invalid policy domain")
        for key in ("gateway4", "gateway6"):
            if group.get(key) and ipaddress.ip_address(group[key]).version != int(key[-1]):
                raise ValueError("Wrong gateway address family")
    return groups


def sqm(value):
    result = value.get("interfaces", [])
    seen = set()
    for entry in result:
        name = interface(entry["interface"])
        if name in seen or not isinstance(entry.get("enabled", True), bool):
            raise ValueError("Invalid SQM interface")
        seen.add(name)
        for key in ("upload_kbit", "download_kbit"):
            if type(entry[key]) is not int or not 64 <= entry[key] <= 10000000:
                raise ValueError("SQM speeds must be explicit kbit/s in 64..10000000")
    return result


def validate_layout(table, layout):
    table = table["partitiontable"]
    parts = table["partitions"]
    if table["label"] != "dos" or table.get("sectorsize", 512) != 512 or len(parts) != 2:
        raise ValueError("Expected the two-partition R2S MBR layout")
    if (parts[0]["start"] != layout["boot_start"] or parts[0]["size"] != layout["boot_sectors"]
            or parts[1]["start"] != layout["root_start"] or any(p["type"] != "83" for p in parts)):
        raise ValueError("R2S partition offsets/types do not match the layout")
    if parts[1]["size"] <= 0:
        raise ValueError("Empty Btrfs partition")
    return parts


def doh_stamp(address, hostname):
    # DNS stamp v1: DoH, DNSSEC property, server IP, no certificate pin,
    # authenticated hostname and HTTPS path. No plaintext DNS bootstrap needed.
    ipaddress.ip_address(address)
    def lp(text):
        data = text.encode("ascii")
        return bytes([len(data)]) + data
    data = b"\x02" + struct.pack("<Q", 1) + lp(address + ":443") + b"\x00" + lp(hostname) + lp("/dns-query")
    return "sdns://" + base64.urlsafe_b64encode(data).decode().rstrip("=")


def doh_config():
    return f'''listen_addresses = ['127.0.0.1:5053']
server_names = ['cloudflare', 'quad9']
max_clients = 250
ipv4_servers = true
ipv6_servers = false
dnscrypt_servers = false
doh_servers = true
require_dnssec = true
timeout = 5000
cache = true
cache_size = 4096
ignore_system_dns = true
netprobe_address = '1.1.1.1:443'

[static.'cloudflare']
stamp = '{doh_stamp("1.1.1.1", "cloudflare-dns.com")}'
[static.'quad9']
stamp = '{doh_stamp("9.9.9.9", "dns.quad9.net")}'
'''


def dns_config(config, groups):
    config = router(config)
    network = ipaddress.ip_interface(config["lan_address"])
    lines = ["# Generated from /etc/r2s/router.json; no resolver from WAN DHCP.",
             "user=dnsmasq", "group=dnsmasq", "dhcp-leasefile=/var/lib/misc/dnsmasq.leases",
             "port=53", "bind-dynamic", "interface=lo", "interface=" + config["lan_bridge"],
             "except-interface=wan0", "no-resolv", "server=127.0.0.1#5053",
             "domain-needed", "bogus-priv", "stop-dns-rebind", "cache-size=4096",
             "local=/" + config["domain"] + "/", "domain=" + config["domain"], "expand-hosts",
             f"dhcp-range={value(config, 'dhcp_start')},{value(config, 'dhcp_end')},{network.netmask},{value(config, 'lease_time')}",
             "dhcp-option=option:router," + str(network.ip), "dhcp-option=option:dns-server," + str(network.ip),
             "dhcp-authoritative", "conf-dir=/run/r2s/dns-blocklists,*.conf"]
    if config.get("ipv6_lan_prefix"):
        lines += ["enable-ra", "dhcp-range=::,constructor=" + config["lan_bridge"] + ",ra-stateless,ra-names,12h"]
    for group in groups:
        if group.get("domains"):
            domains = "/".join(group["domains"])
            number = group["id"]
            lines.append(f"nftset=/{domains}/4#inet#{TABLE}#pbr{number}_4,6#inet#{TABLE}#pbr{number}_6")
    return "\n".join(lines) + "\n"


def value(config, key):
    return config[key]


def connections(config, wan, lans):
    config = router(config)
    interface(wan)
    for name in lans:
        interface(name)
    if wan in lans or wan in config.get("additional_wan", []) or not lans:
        raise ValueError("WAN and LAN ports must be disjoint and LAN cannot be empty")
    bridge = config["lan_bridge"]
    def connection(name, kind, device, body, extra=""):
        identity = uuid.uuid5(uuid.NAMESPACE_DNS, "r2s/" + name)
        return f"[connection]\nid=r2s-{name}\nuuid={identity}\ntype={kind}\ninterface-name={device}\nautoconnect=true\nautoconnect-priority=1000\n{extra}\n{body}"
    wan_body = "[ipv4]\nmethod=auto\nignore-auto-dns=true\nroute-metric=100\n[ipv6]\nmethod=auto\nignore-auto-dns=true\n"
    files = {"wan": connection("wan", "ethernet", wan, wan_body)}
    for index, name in enumerate(config.get("additional_wan", []), 1):
        files[f"wan{index}"] = connection(f"wan{index}", "ethernet", name, wan_body.replace("=100", f"={100 + index * 100}"))
    ipv6 = "method=link-local\n"
    if config.get("ipv6_lan_prefix"):
        subnet = ipaddress.ip_network(config["ipv6_lan_prefix"])
        ipv6 = f"method=manual\naddress1={subnet.network_address + 1}/64\n"
    files["lan"] = connection("lan", "bridge", bridge,
        f"[bridge]\nstp=false\n[ipv4]\nmethod=manual\naddress1={config['lan_address']}\nnever-default=true\n[ipv6]\n{ipv6}never-default=true\n")
    for name in lans:
        files[f"lan-{name}"] = connection(f"lan-{name}", "ethernet", name,
            "[bridge-port]\n[ipv4]\nmethod=disabled\n[ipv6]\nmethod=disabled\n",
            f"master=r2s-lan\nslave-type=bridge\n")
    return files


def blocklist(text, allowed=()):
    domains = set()
    for line in text.splitlines():
        line = line.strip().lower()
        if not line or line.startswith(("#", "!")):
            continue
        if not DOMAIN.fullmatch(line):
            raise ValueError("Blocklist contains an invalid domain")
        domains.add(line)
        if len(domains) > 250000:
            raise ValueError("Blocklist exceeds the 8 GB router's configured limit")
    if not domains:
        raise ValueError("Empty blocklist")
    result = "".join(f"address=/{name}/#\n" for name in sorted(domains))
    for name in sorted(set(allowed)):
        if not DOMAIN.fullmatch(name):
            raise ValueError("Invalid allowlist domain")
        result += f"server=/{name}/#\n"
    return result


def firewall(config, wan, groups, existing=False, known_sets=(), docker_bridges=("docker0",)):
    config = router(config)
    interface(wan)
    lans = json.dumps(config["lan_bridge"])
    wans = ", ".join(json.dumps(interface(name)) for name in [wan] + config.get("additional_wan", []))
    lan = str(ipaddress.ip_interface(config["lan_address"]).network)
    prefix = f"inet {TABLE}"
    lines = [] if existing else [f"add table {prefix}"]
    types = {"input": "filter hook input priority -10; policy drop;",
             "forward": "filter hook forward priority -10; policy drop;",
             "output": "filter hook output priority -10; policy accept;",
             "mark": "filter hook prerouting priority mangle; policy accept;",
             "dns_redirect": "nat hook prerouting priority dstnat - 5; policy accept;",
             "masquerade": "nat hook postrouting priority srcnat - 5; policy accept;"}
    for chain, spec in types.items():
        if existing:
            lines.append(f"flush chain {prefix} {CHAINS[chain]}")
        else:
            lines.append(f"add chain {prefix} {CHAINS[chain]} {{ type {spec} }}")
    def rule(chain, text):
        lines.append(f"add rule {prefix} {CHAINS[chain]} {text}")
    rule("mark", f"ip daddr {lan} return")
    rule("mark", "ip6 daddr { fe80::/10, ff00::/8 } return")
    if config.get("ipv6_lan_prefix"):
        rule("mark", f"ip6 daddr {config['ipv6_lan_prefix']} return")
    rule("mark", "ct mark >= 20993 ct mark <= 21091 meta mark set ct mark")
    for group in groups:
        number = group["id"]
        for family, typename in ((4, "ipv4_addr"), (6, "ipv6_addr")):
            name = f"pbr{number}_{family}"
            if name not in known_sets:
                lines.append(f"add set {prefix} {name} {{ type {typename}; }}")
        mark = 0x5200 + number
        for source in group.get("sources", []):
            field = "ip" if ipaddress.ip_network(source).version == 4 else "ip6"
            rule("mark", f"iifname {lans} {field} saddr {source} meta mark set {mark} ct mark set meta mark")
        for family, field in ((4, "ip"), (6, "ip6")):
            rule("mark", f"iifname {lans} {field} daddr @pbr{number}_{family} meta mark set {mark} ct mark set meta mark")
        if group.get("vpn_only"):
            # Before established/related: an existing flow cannot leak on VPN loss.
            rule("forward", f"iifname {lans} meta nfproto ipv4 ip daddr != {lan} ct mark {mark} oifname != {json.dumps(group['interface'])} counter drop")
            v6_exclusion = "ip6 daddr != { fe80::/10, ff00::/8" + (", " + config["ipv6_lan_prefix"] if config.get("ipv6_lan_prefix") else "") + " }"
            rule("forward", f"iifname {lans} meta nfproto ipv6 {v6_exclusion} ct mark {mark} oifname != {json.dumps(group['interface'])} counter drop")
    rule("input", 'iifname "lo" accept')
    rule("input", "ct state invalid drop")
    rule("input", "ct state established,related accept")
    rule("input", "meta l4proto { icmp, ipv6-icmp } accept")
    rule("input", f"iifname {{ {wans} }} udp sport 67 udp dport 68 accept")
    rule("input", f"iifname {{ {wans} }} udp sport 547 udp dport 546 accept")
    rule("input", f"iifname {lans} ip saddr {lan} tcp dport {{ 22, 53 }} accept")
    rule("input", f"iifname {lans} udp dport {{ 53, 67, 547 }} accept")
    rule("input", f"iifname {lans} meta nfproto ipv6 tcp dport {{ 22, 53 }} accept")
    rule("forward", "ct state invalid drop")
    # Port policies precede established flows and all Docker accept chains.
    rule("forward", f"iifname {lans} meta l4proto {{ tcp, udp }} th dport {{ 53, 853 }} counter drop")
    rule("forward", "ct state established,related accept")
    rule("forward", f"iifname {lans} accept")
    # Stock Docker does its own publication/NAT/connection policy. Permit its
    # bridges through this early chain; DOCKER-USER handles router compatibility.
    for device in docker_bridges:
        interface(device)
        if device == config["lan_bridge"] or device == wan:
            raise ValueError("Docker bridge overlaps a router interface")
        rule("forward", f"iifname {json.dumps(device)} accept")
        rule("forward", f"oifname {json.dumps(device)} accept")
    rule("dns_redirect", f"iifname {lans} meta l4proto {{ tcp, udp }} th dport 53 redirect to :53")
    rule("masquerade", f"ip saddr {lan} oifname {{ {wans} }} masquerade")
    return "\n".join(lines) + "\n"
