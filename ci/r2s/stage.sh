#!/usr/bin/env bash
set -euo pipefail
umask 022
root=$(realpath "$(dirname "$0")/../..")
[[ ${CI:-false} == true && $root == /openwrt && $EUID == 0 ]] || exit 1
cd "$root"
source ci/r2s/chroot-env.sh
export OFFLINE_WORK=yes
export R2S_STAGE=${1:?Specify kernel, uboot, rootfs, assemble or verify}
case "$R2S_STAGE" in kernel|uboot|rootfs|assemble|verify) ;; *) exit 2 ;; esac
mkdir -p _ci/{home,tmp,logs,state} output/{debs,debug} external/cache/{rootfs,ccache} userpatches/overlay
export HOME="$root/_ci/home" TMPDIR="$root/_ci/tmp"
export SOURCE_DATE_EPOCH
SOURCE_DATE_EPOCH=$(< _ci/state/source-date-epoch)
source _ci/state/build.env
git config --global --add safe.directory "$root"
for directory in "kernel/$R2S_KERNEL_COMMIT" "u-boot/$R2S_UBOOT_COMMIT" external/cache/sources/orangepi-firmware-git; do
    git config --global --add safe.directory "$root/$directory"
done
dpkg-query -W -f='${Package}\t${Version}\n' > "_ci/logs/container-packages-$R2S_STAGE.tsv"
cp ci/r2s/config.conf userpatches/config-r2s-debian.conf
touch .ignore_changes

if [[ $R2S_STAGE == kernel || $R2S_STAGE == uboot ]]; then
    mkdir -p toolchains
    python3 -B ci/r2s/pipeline.py import toolchain
    archive=(_ci/cache/toolchain/*.tar.xz)
    [[ ${#archive[@]} == 1 ]]
    python3 -B ci/r2s/toolchain.py "${archive[0]}" --destination toolchains --lock _ci/state/context.json
    touch toolchains/ky-toolchain-linux-glibc-x86_64-v1.0.1/.download-complete
    if [[ $R2S_STAGE == kernel ]]; then ccache --max-size=1792M; else ccache --max-size=256M; fi
    ccache --zero-stats
    touch _ci/state/ccache.ready
fi
if [[ $R2S_STAGE == rootfs || $R2S_STAGE == assemble || $R2S_STAGE == verify ]]; then
    mountpoint -q /proc/sys/fs/binfmt_misc || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
    update-binfmts --enable qemu-riscv64
    grep -q '^enabled' /proc/sys/fs/binfmt_misc/qemu-riscv64
    grep -q '^flags:.*F' /proc/sys/fs/binfmt_misc/qemu-riscv64
fi
start=$(date +%s)
case "$R2S_STAGE" in
    kernel)
        python3 -B ci/r2s/pipeline.py import source-kernel
        python3 -B ci/r2s/pipeline.py sources kernel
        bash build.sh r2s-debian BUILD_OPT=kernel
        python3 -B ci/r2s/kernel.py verify "kernel/$R2S_KERNEL_COMMIT/.config"
        ccache --show-stats > _ci/logs/ccache.txt
        ;;
    uboot)
        python3 -B ci/r2s/pipeline.py import source-uboot
        python3 -B ci/r2s/pipeline.py sources uboot
        bash build.sh r2s-debian BUILD_OPT=u-boot
        ccache --show-stats > _ci/logs/ccache.txt
        ;;
    rootfs)
        python3 -B ci/r2s/pipeline.py import source-firmware
        python3 -B ci/r2s/pipeline.py sources firmware
        if compgen -G 'external/cache/rootfs/*.tar.lz4' >/dev/null; then bash ci/r2s/cache.sh rootfs-check; fi
        # Command-line ROOT_FS_CREATE_ONLY is required: upstream only sets it
        # automatically when selecting rootfs through its interactive menu.
        bash build.sh r2s-debian BUILD_OPT=rootfs ROOT_FS_CREATE_ONLY=yes
        bash ci/r2s/cache.sh rootfs
        ;;
    assemble)
        for component in kernel uboot rootfs; do python3 -B ci/r2s/pipeline.py import "$component"; done
        python3 -B ci/r2s/pipeline.py check-components
        bash ci/r2s/cache.sh rootfs-check
        release=$(< _ci/references/kernel/kernel.release)
        [[ $release =~ ^[a-zA-Z0-9._+-]+$ ]]
        bash build.sh r2s-debian CLEAN_LEVEL= REQUIRE_PREBUILT=yes IMAGE_KERNEL_VERSION="$release"
        test -s _ci/state/raw-image.sha256
        test -f _ci/state/image.assembled
        images=(output/images/*/*.img.gz)
        [[ ${#images[@]} == 1 && -s ${images[0]} ]]
        gzip -t "${images[0]}"
        actual=$(gzip -dc "${images[0]}" | sha256sum | cut -d' ' -f1)
        [[ $actual == "$(cut -d' ' -f1 _ci/state/raw-image.sha256)" ]]
        ;;
    verify)
        for component in kernel uboot rootfs image; do python3 -B ci/r2s/pipeline.py import "$component"; done
        python3 -B ci/r2s/pipeline.py check-components
        images=(_ci/image/*.img.gz)
        [[ ${#images[@]} == 1 && -s ${images[0]} ]]
        (cd _ci/image && sha256sum -c ./*.img.gz.sha)
        mkdir -p .tmp/image-verify
        gzip -dc "${images[0]}" > .tmp/image-verify/r2s-debian.img
        actual=$(sha256sum .tmp/image-verify/r2s-debian.img | cut -d' ' -f1)
        [[ $actual == "$(cut -d' ' -f1 _ci/image/raw-image.sha256)" ]]
        bash ci/r2s/verify-image.sh .tmp/image-verify/r2s-debian.img
        test -f _ci/state/image.verified
        mkdir -p _ci/release/{locks,logs,packages,config,boot,bootloader}
        cp "${images[0]}" "${images[0]}.sha" _ci/image/raw-image.sha256 _ci/release/
        cp _ci/state/*.json _ci/state/build.env _ci/state/source-date-epoch _ci/release/locks/
        cp -r _ci/logs/. _ci/release/logs/
        cp -r output/debs/. _ci/release/packages/
        cp _ci/references/kernel/kernel.config _ci/release/config/
        cp _ci/state/boot/* _ci/release/boot/
        cp _ci/references/uboot/{bootinfo_sd.bin,bootinfo_emmc.bin,FSBL.bin,u-boot-env-default.bin,u-boot-opensbi.itb} _ci/release/bootloader/
        for component in kernel uboot rootfs image; do
            cp "_ci/inbox/$component/manifest.json" "_ci/release/locks/$component.manifest.json"
        done
        (cd _ci/release && find . -type f ! -name sha256sums -print0 | sort -z | xargs -0 sha256sum > sha256sums)
        touch _ci/state/release.ready
        ;;
esac
printf '%s\n' "$(( $(date +%s) - start ))" > "_ci/logs/$R2S_STAGE-seconds.txt"
df -h "$root" > _ci/logs/disk.txt
role=$R2S_STAGE
[[ $role != assemble ]] || role=image
[[ $role != verify ]] || role=release
python3 -B ci/r2s/pipeline.py capture "$role"
