#!/usr/bin/env bash
set -euo pipefail
umask 022
root=$(realpath "$(dirname "$0")/../..")
[[ ${CI:-false} == true && $root == /openwrt ]] || exit 1
cd "$root"
case "${1:-}" in
    rootfs-check)
        test -s external/cache/rootfs/SHA256SUMS
        (cd external/cache/rootfs && sha256sum -c SHA256SUMS)
        ;;
    rootfs)
        shopt -s nullglob
        archives=(external/cache/rootfs/*.tar.lz4)
        [[ ${#archives[@]} == 1 ]] || exit 1
        lz4 -t "${archives[0]}"
        lz4 -dc "${archives[0]}" | tar -tf - > /dev/null
        (cd external/cache/rootfs && sha256sum ./*.tar.lz4 > SHA256SUMS)
        touch _ci/state/rootfs.ready
        ;;
    finish)
        mkdir -p _ci/logs
        if [[ -f _ci/state/ccache.ready ]]; then ccache --show-stats > _ci/logs/ccache.txt; fi
        directories=()
        for directory in external/cache/{ccache,rootfs} _ci/cache/{sources,toolchain}; do
            [[ ! -d $directory ]] || directories+=("$directory")
        done
        if (( ${#directories[@]} )); then du -sh "${directories[@]}" > _ci/logs/cache-sizes.txt; fi
        df -h "$root" > _ci/logs/disk.txt
        # GHA archives only completed stages; never save a live rootfs/mount.
        if [[ -f _ci/state/rootfs.ready ]]; then
            bash ci/r2s/cache.sh rootfs-check
        fi
        ;;
    *) exit 2 ;;
esac
