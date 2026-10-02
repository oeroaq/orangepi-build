#!/usr/bin/env bash
set -euo pipefail
umask 022
root=$(realpath "$(dirname "$0")/../..")
[[ ${CI:-false} == true && $root == /openwrt && $EUID == 0 ]] || exit 1
image=$(realpath "$1")
[[ $image == "$root/.tmp/"*.img ]]
cd "$root"
source ci/r2s/chroot-env.sh
source _ci/state/build.env
mkdir -p _ci/{logs,verify-root,verify-top,verify-tmp,verify-run,state/boot}
target=$(realpath _ci/verify-root)
top=$(realpath _ci/verify-top)
[[ $target == "$root/_ci/verify-root" && $top == "$root/_ci/verify-top" ]]
# Native deployment must fit even a nominal 8 GB eMMC, with update headroom.
(( $(stat -c %s "$image") <= 7000000000 ))
sfdisk --json "$image" > _ci/logs/partition-table.json
PYTHONPATH="$root/package/r2s-platform/root/usr/lib/r2s" python3 -B - <<'PY'
import json, model
layout = json.load(open('package/r2s-platform/root/usr/share/r2s/layout.json'))
model.validate_layout(json.load(open('_ci/logs/partition-table.json')), layout)
PY
loop=''
cleanup()
{
    for virtual in run tmp proc sys dev; do
        mountpoint -q "$target/$virtual" && umount "$target/$virtual"
    done
    mountpoint -q "$target/boot" && umount "$target/boot"
    mountpoint -q "$target" && umount "$target"
    mountpoint -q "$top" && umount "$top"
    [[ -z $loop ]] || losetup -d "$loop"
}
trap cleanup EXIT
loop=$(losetup --find --show --read-only --partscan "$image")
for part in 1 2; do
    device="${loop}p${part}"
    if [[ ! -b $device ]]; then
        for attempt in {1..20}; do
            [[ -r /sys/class/block/${device##*/}/dev ]] && break
            if (( attempt == 20 )); then
                printf 'Missing loop partition node: %s\n' "$device" >&2
                exit 1
            fi
            sleep .25
        done
        numbers=$(< "/sys/class/block/${device##*/}/dev")
        mknod "$device" b "${numbers%:*}" "${numbers#*:}"
    fi
done
[[ $(blkid -s TYPE -o value "${loop}p1") == ext4 ]]
[[ $(blkid -s TYPE -o value "${loop}p2") == btrfs ]]
e2fsck -fn "${loop}p1" > _ci/logs/boot-fsck.txt 2>&1
btrfs check --readonly "${loop}p2" > _ci/logs/btrfs-check.txt 2>&1
mount -o ro,subvolid=5 "${loop}p2" "$top"
for name in @root @data @log @snapshots @docker @containerd; do
    btrfs subvolume show "$top/$name" >> _ci/logs/subvolumes.txt
done
mount -o ro,subvol=@root "${loop}p2" "$target"
mount -o ro,noload "${loop}p1" "$target/boot"
# Guest programs (dnsmasq, TLS, SSH checks) need the runtime device/proc
# interfaces. Bind them only for validation; firmware files stay read-only.
mount --bind /dev "$target/dev"
mount -o remount,bind,ro "$target/dev"
mount --bind /sys "$target/sys"
mount -o remount,bind,ro "$target/sys"
mount -t proc -o ro proc "$target/proc"
# systemd-analyze v257 creates /tmp/systemd-analyze-* while checking aliases.
# Keep these writes in the bind-mounted workspace, not in the image filesystem.
mount --bind "$root/_ci/verify-tmp" "$target/tmp"
mount --bind "$root/_ci/verify-run" "$target/run"
chroot "$target" /bin/sh -eu -c '
    . /etc/os-release
    test "$ID" = debian && test "$VERSION_ID" = 13
    test "$(dpkg --print-architecture)" = riscv64
    test -x /bin/sh
    for file in /boot/Image /boot/uInitrd /boot/boot.scr /boot/orangepiEnv.txt /boot/dtb/ky/x1_orangepi-r2s.dtb; do
        test -s "$file"
    done
    grep -q "fdtfile=ky/x1_orangepi-r2s.dtb" /boot/orangepiEnv.txt
    grep -q "rootfstype=btrfs" /boot/orangepiEnv.txt
    grep -q "rootflags=subvol=@root" /boot/orangepiEnv.txt
    for package in linux-image-current-ky linux-u-boot-orangepir2s-current r2s-platform; do
        test "$(dpkg-query -W -f="\${Status}" "$package")" = "install ok installed"
    done
    test ! -e /etc/docker/daemon.json
    test -s /usr/lib/firmware/esos.elf
    test -x /etc/initramfs-tools/hooks/r2s-firmware
    dpkg-query -S /usr/lib/firmware/esos.elf | grep -q "^r2s-platform:"
    dpkg-query -S /etc/initramfs-tools/hooks/r2s-firmware | grep -q "^r2s-platform:"
    python3 -B /usr/lib/r2s/runtime.py check
    visudo -cf /etc/sudoers
    dnsmasq --version | grep -w nftset
    dnscrypt-proxy -config /etc/r2s/doh.toml -check
    collectd -t -C /etc/r2s/collectd.conf
    ! find /etc/ssh -name "ssh_host_*_key" -type f | grep .
    ! find /opt /root /var/cache/apt/archives -name "*.deb" -type f | grep .
    test ! -s /etc/machine-id
    getent shadow admin | grep -q "^admin:!"
    getent shadow root | grep -q "^root:!"
    test ! -e /home/admin/.ssh/authorized_keys
    for package in build-essential gcc g++ cmake dkms python3-dev libpython3-dev adblock luci cockpit; do
        test "$(dpkg-query -W -f="\${Status}" "$package" 2>/dev/null || true)" != "install ok installed"
    done
' > _ci/logs/image-validation.txt 2>&1
while IFS= read -r package; do
    [[ -z $package || $package == \#* ]] && continue
    [[ $(chroot "$target" dpkg-query -W '-f=${Status}' "$package") == 'install ok installed' ]]
done < ci/r2s/packages.list
kernel_tree="kernel/$R2S_KERNEL_COMMIT"
kernel_config="$kernel_tree/.config"
kernel_image="$kernel_tree/arch/riscv/boot/Image"
kernel_dtb="$kernel_tree/arch/riscv/boot/dts/ky/x1_orangepi-r2s.dtb"
uboot_tree="u-boot/$R2S_UBOOT_COMMIT"
if [[ -d _ci/references/kernel ]]; then
    kernel_config=_ci/references/kernel/kernel.config
    kernel_image=_ci/references/kernel/Image
    kernel_dtb=_ci/references/kernel/x1_orangepi-r2s.dtb
    uboot_tree=_ci/references/uboot
fi
python3 -B ci/r2s/kernel.py verify "$kernel_config"
if [[ -f _ci/references/kernel/kernel.release ]]; then
    expected_release=$(< _ci/references/kernel/kernel.release)
    actual_release=$(chroot "$target" /bin/sh -c 'set -- /lib/modules/*; test "$#" -eq 1; basename "$1"')
    [[ $expected_release == "$actual_release" ]]
fi
chroot "$target" /bin/sh -eu -c '
    set -- /lib/modules/*
    test "$#" -eq 1
    kernel=${1##*/}
    for module in vxlan macvlan wireguard overlay 8021q bridge br_netfilter bonding vrf veth \
        sch_cake sch_fq_codel sch_htb sch_ingress ifb cls_u32 cls_flower cls_matchall \
        act_mirred act_police nf_tables nf_conntrack nf_nat nft_fib_inet nft_tproxy \
        xfrm_user esp4 esp6 pppoe; do
        vermagic=$(modinfo -k "$kernel" -F vermagic "$module")
        test "${vermagic%% *}" = "$kernel"
    done
' >> _ci/logs/image-validation.txt 2>&1
chroot "$target" dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > _ci/logs/image-packages.tsv
python3 -B ci/r2s/verify-config.py "$target" "$top" "${loop}p1" "${loop}p2"
python3 -B ci/r2s/boot.py verify "$target" > _ci/logs/boot-validation.txt
chroot "$target" systemd-analyze verify \
    r2s-grow.service r2s-network.service r2s-refresh.service r2s-links.service \
    r2s-dns.service r2s-doh.service r2s-firstboot.service r2s-restore.service \
    r2s-metrics.service r2s-vnstat.service r2s-blocklist.service \
    > _ci/logs/systemd-validation.txt 2>&1
for file in /boot/Image /boot/dtb/ky/x1_orangepi-r2s.dtb; do
    installed=$(chroot "$target" sha256sum "$file" | cut -d' ' -f1)
    case "$file" in
        /boot/Image) compiled=$(sha256sum "$kernel_image" | cut -d' ' -f1) ;;
        *) compiled=$(sha256sum "$kernel_dtb" | cut -d' ' -f1) ;;
    esac
    [[ $installed == "$compiled" ]]
done
cp "$kernel_config" _ci/logs/kernel.config
cp "$target/boot/boot.scr" "$target/boot/boot.cmd" "$target/boot/orangepiEnv.txt" _ci/state/boot/
chroot "$target" sh -c 'readlink -f /boot/Image; readlink -f /boot/uInitrd; readlink -f /boot/dtb/ky/x1_orangepi-r2s.dtb' |
while IFS= read -r file; do
    chroot "$target" cat "$file" > "_ci/state/boot/$(basename "$file")"
done
python3 -B - "$image" "$uboot_tree" <<'PY'
from pathlib import Path
import sys
image, tree = Path(sys.argv[1]), Path(sys.argv[2])
with image.open('rb') as disk:
    for name, sector, maximum in [('FSBL.bin', 256, 512), ('u-boot-env-default.bin', 768, 128), ('u-boot-opensbi.itb', 1664, 6144)]:
        data = (tree / name).read_bytes()
        assert 0 < len(data) <= maximum * 512, f'{name} exceeds its boot area'
        disk.seek(sector * 512)
        assert disk.read(len(data)) == data, f'{name} differs from this build'
PY
sha256sum "$image" | awk '{print $1 "  r2s-debian.img"}' > _ci/state/raw-image.sha256
printf '%s\n' 'Static image validation passed; hardware boot/eMMC tests are still required.' > _ci/state/image.verified
