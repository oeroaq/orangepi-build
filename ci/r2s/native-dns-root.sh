#!/usr/bin/env bash
# QEMU user mode cannot translate NETLINK_NETFILTER. Test nftset integration
# natively with the exact dnsmasq package version from the locked snapshot.
set -euo pipefail
root=$(realpath "$(dirname "$0")/../..")
[[ ${CI:-false} == true && $root == /openwrt && $EUID == 0 ]]
source "$root/_ci/state/build.env"
target="$root/_ci/dns-native"
[[ ! -e $target ]]
mkdir -p "$root/_ci/dns-native-tools"
# Native debootstrap also needs the guest temporary environment, without
# putting its host-side downloads or logs outside the workspace.
cp "$root/ci/r2s/chroot-command.sh" "$root/_ci/dns-native-tools/chroot"
chmod 0755 "$root/_ci/dns-native-tools/chroot"
PATH="$root/_ci/dns-native-tools:$PATH" debootstrap --variant=minbase --arch=amd64 \
    --include=dnsmasq-base --keyring=/usr/share/keyrings/debian-archive-keyring.gpg \
    trixie "$target" "https://snapshot.debian.org/archive/debian/$R2S_DEBIAN_SNAPSHOT/"
source "$root/ci/r2s/chroot-env.sh"
expected=$(chroot "$root/_ci/verify-root" dpkg-query -W '-f=${Version}' dnsmasq-base)
actual=$(chroot "$target" dpkg-query -W '-f=${Version}' dnsmasq-base)
[[ $expected == "$actual" ]]
mkdir -p "$target/run/r2s" "$target/var/lib/misc" "$target/dev"
printf 'Native dnsmasq reference matches image version: %s\n' "$actual"
