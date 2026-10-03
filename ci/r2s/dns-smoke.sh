#!/usr/bin/env bash
# Real image binaries, read-only root and runtime exceptions, isolated network.
set -euo pipefail
root=$(realpath "$(dirname "$0")/../..")
target=$(realpath "$1")
[[ ${CI:-false} == true && $root == /openwrt && $target == "$root/_ci/verify-root" && $EUID == 0 ]] || exit 1
source "$root/ci/r2s/chroot-env.sh"
directory="$root/_ci/dns-smoke"
mode=${2:---lan}
[[ $mode == --lan || $mode == --doh ]]
mkdir -p "$directory/run" "$directory/leases" "$directory/doh"
client='' daemon='' upstream=''
cleanup()
{
    for process in "$daemon" "$upstream" "$client"; do
        [[ -z $process ]] || kill "$process" 2>/dev/null || true
    done
    for process in "$daemon" "$upstream" "$client"; do
        [[ -z $process ]] || wait "$process" 2>/dev/null || true
    done
}
trap cleanup EXIT
if [[ $mode == --doh ]]; then
    uid=$(chroot "$target" id -u r2s-doh)
    gid=$(chroot "$target" id -g r2s-doh)
    chown "$uid:$gid" "$directory/doh"
    chmod 0750 "$directory/doh"
    mount --bind "$directory/doh" "$target/run/r2s-doh"
    command chroot --userspec="$uid:$gid" "$target" /usr/bin/env HOME=/run/r2s-doh TMPDIR=/tmp \
        /bin/sh -c 'cd /run/r2s-doh; exec /usr/sbin/dnscrypt-proxy -config /etc/r2s/doh.toml' \
        > "$root/_ci/logs/doh-smoke-daemon.txt" 2>&1 &
    daemon=$!
    python3 -B "$root/ci/r2s/dns-probe.py" doh
    exit 0
fi
ip link set lo up
ip link add br-lan type bridge
ip address add 10.0.0.1/24 dev br-lan
ip link set br-lan up
unshare --net sleep 300 & client=$!
sleep .5
ip link add r2s-server type veth peer name r2s-client
ip link set r2s-client netns "$client"
ip link set r2s-server master br-lan
ip link set r2s-server up
nsenter -t "$client" -n ip link set lo up
nsenter -t "$client" -n ip link set r2s-client address 02:00:00:00:01:23
nsenter -t "$client" -n ip link set r2s-client up
mkdir -p "$directory/run/dns-blocklists" "$target/run/r2s" "$target/var/lib/misc"
# Replicate ProtectSystem=strict /run, with only /run/r2s writable.
mount --bind "$target/run" "$target/run"
mount -o remount,bind,ro "$target/run"
mount --bind "$directory/run" "$target/run/r2s"
mount --bind "$directory/leases" "$target/var/lib/misc"
python3 -B - "$root" "$target" "$directory" <<'PY'
import json, sys
from pathlib import Path
root, image, directory = map(Path, sys.argv[1:])
sys.path.insert(0, str(root / 'package/r2s-platform/root/usr/lib/r2s'))
import model
config = json.loads((image / 'etc/r2s/router.json').read_text())
groups = model.policies({'groups':[{'id':1,'interface':'wg0','domains':['example.com']}]})
(directory / 'run/dnsmasq.conf').write_text(model.dns_config(config, groups) + 'host-record=smoke.home.arpa,10.0.0.1\n')
(directory / 'run/dns-blocklists/smoke.conf').write_text('address=/blocked.example/#\n')
PY
nft add table inet r2s_router
nft add set inet r2s_router pbr1_4 '{ type ipv4_addr; }'
nft add set inet r2s_router pbr1_6 '{ type ipv6_addr; }'
python3 -B "$root/ci/r2s/dns-probe.py" upstream & upstream=$!
chroot "$target" dnsmasq --keep-in-foreground --conf-file=/run/r2s/dnsmasq.conf \
    > "$root/_ci/logs/dns-smoke-daemon.txt" 2>&1 &
daemon=$!
for attempt in {1..50}; do
    [[ -s $directory/run/dnsmasq.pid ]] && break
    kill -0 "$daemon"
    if (( attempt == 50 )); then
        printf 'Image dnsmasq did not create its protected pidfile after %s checks\n' "$attempt" >&2
        exit 1
    fi
    sleep .1
done
[[ -s $directory/run/dnsmasq.pid ]]
lease=$(nsenter -t "$client" -n python3 -B "$root/ci/r2s/dns-probe.py" dhcp)
[[ $lease =~ ^10\.0\.0\.(1[0-9][0-9])$ ]]
nsenter -t "$client" -n ip address add "$lease/24" dev r2s-client
nsenter -t "$client" -n python3 -B "$root/ci/r2s/dns-probe.py" dns
nft -j list set inet r2s_router pbr1_4 | python3 -B -c '
import json,sys
data=json.load(sys.stdin)
assert any("192.0.2.123" in item.get("set",{}).get("elem",[]) for item in data["nftables"])
print("Image dnsmasq populated its PBR nftset")'
[[ -s $directory/leases/dnsmasq.leases ]]
printf '%s\n' 'Image DHCP ACK, protected pidfile, leases, LAN DNS and nftset tests passed'
