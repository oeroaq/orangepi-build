"""Offline regressions for the router configuration, filesystem and kernel contract."""
import base64
import copy
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "package/r2s-platform/root"
sys.path.insert(0, str(PLATFORM / "usr/lib/r2s"))
import model
import kernel


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


installer = module("r2s_installer_tests", PLATFORM / "usr/lib/r2s/install.py")
verifier = module("r2s_config_verifier", ROOT / "ci/r2s/verify-config.py")


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((PLATFORM / "etc/r2s/router.json").read_text())

    def test_network_keeps_wan_out_of_bridge_and_dhcp_pool(self):
        profiles = model.connections(self.config, "eth0", ["eth1", "enp1s0", "enp2s0"])
        self.assertIn("method=auto", profiles["wan"])
        self.assertNotIn("master=", profiles["wan"])
        self.assertIn("address1=10.0.0.1/24", profiles["lan"])
        for name in ("eth1", "enp1s0", "enp2s0"):
            self.assertIn("master=r2s-lan", profiles["lan-" + name])
        with self.assertRaises(ValueError):
            model.connections(self.config, "eth0", ["eth0"])
        dns = model.dns_config(self.config, [])
        self.assertIn("10.0.0.100,10.0.0.199,255.255.255.0,12h", dns)
        self.assertIn("no-resolv", dns)
        self.assertIn("server=127.0.0.1#5053", dns)

    def test_invalid_network_input_cannot_inject_generated_configuration(self):
        cases = [("lan_bridge", 'br-lan"; flush ruleset'), ("domain", "home.arpa\nserver=8.8.8.8"),
                 ("dhcp_start", "10.0.0.1"), ("dhcp_end", "10.0.1.1"),
                 ("ipv6_lan_prefix", "fe80::/64"), ("ipv6_lan_prefix", "::/64")]
        for key, value in cases:
            config = dict(self.config, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                model.router(config)

    def test_dns_stamps_use_fixed_ips_authenticated_hosts_and_doh(self):
        stamp = model.doh_stamp("1.1.1.1", "cloudflare-dns.com")[7:]
        data = base64.urlsafe_b64decode(stamp + "=" * (-len(stamp) % 4))
        self.assertEqual(data[0], 2)
        self.assertEqual(struct.unpack("<Q", data[1:9])[0], 1)
        self.assertIn(b"1.1.1.1:443", data)
        self.assertIn(b"cloudflare-dns.com", data)
        self.assertTrue(data.endswith(b"/dns-query"))
        config = model.doh_config()
        self.assertNotIn("https://raw", config)
        self.assertIn("doh_servers = true", config)
        self.assertIn("listen_addresses = ['127.0.0.1:5053']", config)

    def test_blocklists_are_normalized_bounded_and_allow_exceptions(self):
        text = model.blocklist("# header\nAds.Example.com\nads.example.com\ntracker.example.net\n", ["safe.example.com"])
        self.assertEqual(text.count("address=/ads.example.com/#"), 1)
        self.assertIn("server=/safe.example.com/#", text)
        for bad in ("", "example.com\ndhcp-script=/bin/sh", "*.example.com", "localhost", "127.0.0.1 example.com"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                model.blocklist(bad)

    def test_vpn_killswitch_precedes_established_accept(self):
        groups = model.policies({"groups": [{"id": 1, "interface": "wg0", "vpn_only": True,
                                            "sources": ["10.0.0.20/32"], "domains": ["example.com"]}]})
        rules = model.firewall(self.config, "eth0", groups)
        drop = rules.index('ct mark 20993 oifname != "wg0" counter drop')
        established = rules.index('r2s_forward ct state established,related accept')
        self.assertLess(drop, established)
        self.assertIn("ct mark >= 20993", rules)
        self.assertIn("ip daddr != 10.0.0.0/24", rules)
        self.assertIn("ip6 daddr != { fe80::/10, ff00::/8 }", rules)

    def test_firewall_does_not_flush_dynamic_sets_or_allow_wan_to_lan_as_docker(self):
        groups = model.policies({"groups": [{"id": 2, "interface": "wg0", "domains": ["example.com"]}]})
        rules = model.firewall(self.config, "eth0", groups, existing=True,
                               known_sets=["pbr2_4", "pbr2_6"], docker_bridges=["docker0", "br-123456abcdef"])
        self.assertNotIn("flush ruleset", rules)
        self.assertNotIn("flush set", rules)
        self.assertNotIn("add set", rules)
        self.assertNotIn('"br-*"', rules)
        self.assertNotIn('oifname "br-lan" accept', rules)
        self.assertIn('oifname "br-123456abcdef" accept', rules)
        self.assertLess(rules.index("th dport { 53, 853 } counter drop"),
                        rules.index('r2s_forward ct state established,related accept'))

    def test_nft_chain_identifiers_are_plain_and_not_lexer_keywords(self):
        rules = model.firewall(self.config, "eth0", [])
        reload = model.firewall(self.config, "eth0", [], existing=True)
        names = {"r2s_input", "r2s_forward", "r2s_output", "r2s_mark", "r2s_dns_redirect", "r2s_masquerade"}
        created = set()
        for text in (rules, reload):
            for line in text.splitlines():
                if line.startswith(("add chain ", "add rule ", "flush chain ")):
                    name = line.split()[4]
                    self.assertIn(name, names)
                    self.assertRegex(name, r"^[A-Za-z_][A-Za-z0-9_]*$")
                    if line.startswith("add chain "):
                        created.add(name)
        self.assertEqual(created, names)
        self.assertIn('flush chain inet r2s_router r2s_mark', reload)
        self.assertIn('r2s_mark { type filter hook prerouting priority mangle;', rules)
        self.assertIn('r2s_masquerade { type nat hook postrouting priority srcnat - 5;', rules)
        self.assertIn('r2s_masquerade ip saddr 10.0.0.0/24 oifname { "eth0" } masquerade', rules)

    def test_policy_ids_and_sqm_parameters_require_explicit_valid_values(self):
        for groups in ([{"id": 1, "interface": "wg0"}] * 2,
                       [{"id": True, "interface": "wg0"}],
                       [{"id": 1, "interface": "wg0", "domains": ["x\nfoo"]}]):
            with self.assertRaises(ValueError):
                model.policies({"groups": groups})
        self.assertEqual(model.sqm({"interfaces": []}), [])
        with self.assertRaises(ValueError):
            model.sqm({"interfaces": [{"interface": "eth0", "upload_kbit": 0, "download_kbit": 10000}]})

    def test_layout_requires_fixed_start_but_accepts_growth(self):
        layout = json.loads((PLATFORM / "usr/share/r2s/layout.json").read_text())
        table = {"partitiontable": {"label": "dos", "sectorsize": 512, "partitions": [
            {"start": layout["boot_start"], "size": layout["boot_sectors"], "type": "83"},
            {"start": layout["root_start"], "size": 12000000, "type": "83"}]}}
        model.validate_layout(table, layout)
        table["partitiontable"]["partitions"][1]["size"] += 1000000
        model.validate_layout(table, layout)
        table["partitiontable"]["partitions"][1]["start"] += 1
        with self.assertRaises(ValueError):
            model.validate_layout(table, layout)


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "_ci/tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.assertIn((ROOT / "_ci/tests").resolve(), self.root.resolve().parents)
        self.temporary.cleanup()

    def test_kernel_contract_rejects_missing_symbol_and_wrong_builtin_mode(self):
        file = self.root / "config"
        file.write_text("\n".join(f"{symbol}={modes[0]}" for symbol, modes in kernel.requirements()))
        kernel.verify(file)
        original = file.read_text()
        file.write_text(original.replace("CONFIG_BTRFS_FS=y", "CONFIG_BTRFS_FS=m"))
        with self.assertRaises(ValueError):
            kernel.verify(file)
        file.write_text(original.replace("CONFIG_NET_SCH_INGRESS=m", "# CONFIG_NET_SCH_INGRESS is not set"))
        with self.assertRaises(ValueError):
            kernel.verify(file)

    def test_installer_checks_exact_byte_ranges_and_never_copies_past_input(self):
        source, target = self.root / "source", self.root / "target"
        source.write_bytes(b"1234kernel5678")
        target.write_bytes(b"." * 20)
        installer.copy_bytes(source, target, 6, 4, 10)
        self.assertEqual(target.read_bytes()[10:16], b"kernel")
        self.assertEqual(installer.digest(source, 6, 4), installer.digest(target, 6, 10))
        with self.assertRaises(ValueError):
            installer.digest(source, 99)
        with self.assertRaises(ValueError):
            installer.copy_bytes(source, target, 99)

    def test_installer_rejects_nonadjacent_exchange_before_any_syscall(self):
        left, right = self.root / "a/root", self.root / "b/root"
        with self.assertRaises(ValueError):
            installer.exchange_roots(left, right)


if __name__ == "__main__":
    unittest.main()
