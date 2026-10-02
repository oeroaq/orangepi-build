#!/usr/bin/env bash
set -euo pipefail
umask 022
root=$(realpath "$(dirname "$0")/../..")
[[ ${CI:-false} == true && $root == /openwrt && $EUID == 0 ]] || exit 1
volumes=('@root' '@data' '@log' '@snapshots' '@docker' '@containerd')
paths=('/' '/data' '/var/log' '/.snapshots' '/var/lib/docker' '/var/lib/containerd')
case "${1:-}" in
    create)
        [[ $2 =~ ^/dev/loop[0-9]+p2$ && $(blkid -s TYPE -o value "$2") == btrfs ]] || exit 1
        mkdir -p "$root/_ci/btrfs-top"
        top=$(realpath "$root/_ci/btrfs-top")
        [[ $top == "$root/_ci/btrfs-top" ]]
        mount -o subvolid=5 "$2" "$top"
        trap 'umount "$top"' EXIT
        for name in "${volumes[@]}"; do btrfs subvolume create "$top/$name"; done
        id=$(btrfs subvolume show "$top/@root" | awk '/Subvolume ID:/ {print $3}')
        [[ $id =~ ^[0-9]+$ ]]
        btrfs subvolume set-default "$id" "$top"
        ;;
    mount)
        [[ $2 =~ ^/dev/loop[0-9]+p2$ ]] || exit 1
        target=$(realpath "$3")
        source=$(realpath "$4")
        [[ $target == "$root/.tmp/mount-"* && $source == "$root/.tmp/rootfs-"* ]] || exit 1
        uuid=$(blkid -s UUID -o value "$2")
        [[ $uuid =~ ^[a-f0-9-]+$ ]]
        for index in 1 2 3 4 5; do
            mkdir -p "$target${paths[$index]}"
            mount -o "subvol=${volumes[$index]},compress=zstd:3,noatime" "$2" "$target${paths[$index]}"
            printf 'UUID=%s %s btrfs subvol=%s,compress=zstd:3,noatime 0 0\n' \
                "$uuid" "${paths[$index]}" "${volumes[$index]}" >> "$source/etc/fstab"
        done
        # The vendor DTB declares 4 GiB; this R2S has 2 GiB. Keep the early
        # kernel messages while checking the hardware boot. The UART console
        # has been validated; let it replace SBI instead of printing twice.
        sed -i -E '/^(verbosity|console|earlycon|extraargs)=/d' "$source/boot/orangepiEnv.txt"
        printf '\n%s\n' 'verbosity=7' 'console=serial' 'earlycon=on' \
            'extraargs=rootflags=subvol=@root mem=2G ignore_loglevel' \
            >> "$source/boot/orangepiEnv.txt"
        ;;
    unmount)
        target=$(realpath "$2")
        [[ $target == "$root/.tmp/mount-"* ]] || exit 1
        for index in 5 4 3 2 1; do
            umount "$target${paths[$index]}"
        done
        ;;
    *) exit 2 ;;
esac
