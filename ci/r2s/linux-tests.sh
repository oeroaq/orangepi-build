#!/usr/bin/env bash
set -euo pipefail
root=$(realpath "$(dirname "$0")/../..")
[[ ${CI:-false} == true && $root == /openwrt && $EUID == 0 ]] || exit 1
export PYTHONPATH="$root/package/r2s-platform/root/usr/lib/r2s"
directory="$root/_ci/linux-tests"
if [[ ${1:-} == --network ]]; then
    client='' isp='' dns=''
    cleanup_network()
    {
        for pid in "$dns" "$client" "$isp"; do [[ -z $pid ]] || kill "$pid" 2>/dev/null || true; done
    }
    trap cleanup_network EXIT
    ip link set lo up
    unshare --net sleep 3600 & client=$!
    unshare --net sleep 3600 & isp=$!
    sleep .5
    ip link add br-lan type bridge
    ip link set br-lan up
    ip address add 10.0.0.1/24 dev br-lan
    ip link add lan-port type veth peer name client-port
    ip link set client-port netns "$client"
    ip link set lan-port master br-lan
    ip link set lan-port up
    ip link add eth0 type veth peer name isp-port
    ip link set isp-port netns "$isp"
    ip address add 192.0.2.1/24 dev eth0
    ip link set eth0 up
    nsenter -t "$client" -n ip link set lo up
    nsenter -t "$client" -n ip address add 10.0.0.2/24 dev client-port
    nsenter -t "$client" -n ip link set client-port up
    nsenter -t "$client" -n ip route add default via 10.0.0.1
    nsenter -t "$isp" -n ip link set lo up
    nsenter -t "$isp" -n ip address add 192.0.2.2/24 dev isp-port
    nsenter -t "$isp" -n ip link set isp-port up
    nsenter -t "$isp" -n ip route add 10.0.0.0/24 via 192.0.2.1
    sysctl -q -w net.ipv4.ip_forward=1
    python3 -B - "$root" "$directory/firewall.nft" <<'PY'
import json, model, sys
from pathlib import Path
config = json.loads((Path(sys.argv[1]) / 'package/r2s-platform/root/etc/r2s/router.json').read_text())
Path(sys.argv[2]).write_text(model.firewall(config, 'eth0', []))
PY
    nft -c -f "$directory/firewall.nft"
    nft -f "$directory/firewall.nft"
    nsenter -t "$client" -n ping -c 1 -W 2 192.0.2.2
    # Simulate stock Docker's FORWARD policy. Our compatibility chain must
    # preserve routing without changing any daemon configuration.
    iptables -N DOCKER-USER
    iptables -A FORWARD -j DOCKER-USER
    iptables -P FORWARD DROP
    iptables -A DOCKER-USER -i br-lan -j ACCEPT
    iptables -A DOCKER-USER -o br-lan -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    nsenter -t "$client" -n ping -c 1 -W 2 192.0.2.2
    if nsenter -t "$isp" -n ping -c 1 -W 1 10.0.0.2; then
        printf '%s\n' 'Unexpected new WAN -> LAN forwarding' >&2
        exit 1
    fi
    python3 -B -c 'import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind(("10.0.0.1",53)); data,peer=s.recvfrom(512); s.sendto(b"DNS_OK",peer)' & dns=$!
    sleep .25
    nsenter -t "$client" -n python3 -B -c 'import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(3); s.sendto(b"probe",("192.0.2.2",53)); assert s.recv(512)==b"DNS_OK"'
    wait "$dns"; dns=''
    # The VPN killswitch must drop traffic even if a flow was established
    # before the policy was installed and the tunnel is unavailable.
    python3 -B - "$root" "$directory/firewall.nft" <<'PY'
import json, model, sys
from pathlib import Path
config = json.loads((Path(sys.argv[1]) / 'package/r2s-platform/root/etc/r2s/router.json').read_text())
groups = model.policies({'groups':[{'id':1,'interface':'wg0','sources':['10.0.0.2/32'],'vpn_only':True}]})
Path(sys.argv[2]).write_text(model.firewall(config, 'eth0', groups, existing=True))
PY
    nft -c -f "$directory/firewall.nft"
    nft -f "$directory/firewall.nft"
    if nsenter -t "$client" -n ping -c 1 -W 1 192.0.2.2; then
        printf '%s\n' 'VPN-only client leaked to WAN with tunnel down' >&2
        exit 1
    fi
    exit 0
fi
[[ ! -e $directory ]] || { printf '%s\n' 'Refusing to overwrite Linux test fixtures'; exit 1; }
mkdir -p "$directory/top" "$root/_ci/logs"
directory=$(realpath "$directory")
[[ $directory == "$root/_ci/linux-tests" ]]
loop=''
cleanup()
{
    mountpoint -q "$directory/top" && umount "$directory/top"
    [[ -z $loop ]] || losetup -d "$loop"
}
trap cleanup EXIT
truncate -s 256M "$directory/btrfs.img"
loop=$(losetup --find --show "$directory/btrfs.img")
mkfs.btrfs -q "$loop"
mount -o subvolid=5 "$loop" "$directory/top"
btrfs subvolume create "$directory/top/@root"
btrfs subvolume create "$directory/top/@root.new"
btrfs subvolume create "$directory/top/@data"
printf 'old\n' > "$directory/top/@root/version"
printf 'new\n' > "$directory/top/@root.new/version"
printf 'persistent\n' > "$directory/top/@data/value"
python3 -B - "$directory/top" <<'PY'
from pathlib import Path
import importlib.util, sys
spec = importlib.util.spec_from_file_location('r2s_install', '/openwrt/package/r2s-platform/root/usr/lib/r2s/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
root = Path(sys.argv[1])
installer.exchange_roots(root / '@root', root / '@root.new')
assert (root / '@root/version').read_text() == 'new\n'
assert (root / '@data/value').read_text() == 'persistent\n'
installer.exchange_roots(root / '@root', root / '@root.new')
assert (root / '@root/version').read_text() == 'old\n'
PY
umount "$directory/top"
losetup -d "$loop"; loop=''
# Grow an offline image and then its filesystem, checking metadata afterwards.
truncate -s 320M "$directory/btrfs.img"
loop=$(losetup --find --show "$directory/btrfs.img")
mount -o subvolid=5 "$loop" "$directory/top"
btrfs filesystem resize max "$directory/top"
[[ $(< "$directory/top/@data/value") == persistent ]]
umount "$directory/top"
btrfs check --readonly "$loop"
losetup -d "$loop"; loop=''
unshare --net bash "$root/ci/r2s/linux-tests.sh" --network
printf '%s\n' 'Linux Btrfs exchange/growth and isolated routing/firewall tests passed.' > "$root/_ci/logs/linux-tests.txt"
