#!/usr/bin/env bash
set -euo pipefail
umask 022
root=$(realpath "$(dirname "$0")/../..")
image=$(realpath "$1")
[[ ${CI:-false} == true && $root == /openwrt && $image == "$root/.tmp/rootfs-"* && $EUID == 0 ]] || exit 1
cd "$root"
source ci/r2s/chroot-env.sh
package=$(python3 -B ci/r2s/build-package.py)
# dpkg-deb writes its progress to stdout; the final line contains our package.
package=${package##*$'\n'}
[[ $package == "$root/output/debs/r2s-platform_"*.deb && -s $package ]]
cp "$package" "$image/tmp/r2s-platform.deb"
chroot "$image" dpkg -i /tmp/r2s-platform.deb
python3 -B ci/r2s/boot.py install "$image"
chroot "$image" sh -eu -c '
    rm -f /tmp/r2s-platform.deb
    passwd -l root
    passwd -l admin
    for unit in dnsmasq.service dnscrypt-proxy.service dnscrypt-proxy.socket \
        systemd-resolved.service systemd-networkd.service ssh.socket vnstat.service \
        collectd.service strongswan-starter.service strongswan.service \
        orangepi-firstrun.service orangepi-firstrun-config.service orangepi-resize-filesystem.service \
        orangepi-zram-config.service orangepi-ramlog.service; do
        systemctl disable "$unit" 2>/dev/null || true
        systemctl mask "$unit"
    done
    systemctl enable NetworkManager.service ssh.service chrony.service docker.service
    systemctl set-default multi-user.target
    # Generated configuration and identities must not leak into the firmware.
    rm -f /etc/ssh/ssh_host_*
    : > /etc/machine-id
    apt-get clean
    rm -f /var/cache/apt/archives/*.deb /opt/*.deb
    printf "nameserver 127.0.0.1\n" > /etc/resolv.conf
    visudo -cf /etc/sudoers
    python3 -B /usr/lib/r2s/runtime.py check
    dnsmasq --version | grep -w nftset
    dnscrypt-proxy -config /etc/r2s/doh.toml -check
    collectd -t -C /etc/r2s/collectd.conf
    # Reproducible build inputs, but normal Debian security updates on the router.
    cat > /etc/apt/sources.list <<EOF
deb https://deb.debian.org/debian trixie main contrib non-free non-free-firmware
deb https://deb.debian.org/debian trixie-updates main contrib non-free non-free-firmware
deb https://security.debian.org/debian-security trixie-security main contrib non-free non-free-firmware
EOF
    dpkg-query -W -f="\${Package}\n" > /usr/share/r2s/base-packages
'
