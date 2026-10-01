#!/usr/bin/env bash
set -euo pipefail
umask 022

root=$(realpath "$(dirname "$0")/../..")
[[ ${CI:-false} == true && $root == /openwrt && $EUID == 0 ]] || {
    printf '%s\n' 'Run this script in the disposable GitHub Actions build container.' >&2
    exit 1
}
cd "$root"
mkdir -p _ci/{tmp,home,state,logs} toolchains userpatches/overlay output/debug external/cache/{rootfs,ccache}
export HOME="$root/_ci/home" TMPDIR="$root/_ci/tmp"
git config --global --add safe.directory "$root"
source _ci/state/build.env
for directory in "kernel/$R2S_KERNEL_COMMIT" "u-boot/$R2S_UBOOT_COMMIT" \
    external/cache/sources/orangepi-firmware-git; do
    git config --global --add safe.directory "$root/$directory"
done
export SOURCE_DATE_EPOCH
SOURCE_DATE_EPOCH=$(git -C "kernel/$R2S_KERNEL_COMMIT" show -s --format=%ct HEAD)
printf '%s\n' "$SOURCE_DATE_EPOCH" > _ci/state/source-date-epoch
dpkg-query -W -f='${Package}\t${Version}\n' > _ci/logs/build-container-packages.tsv

# Archive restored from cache still has to pass the vendor's versioned checksum.
archive="$root/_ci/cache/toolchain/ky-toolchain-linux-glibc-x86_64-v1.0.1.tar.xz"
python3 - "$archive" <<'PY'
import pathlib, posixpath, sys, tarfile
archive = pathlib.Path(sys.argv[1])
with tarfile.open(archive, 'r:xz') as source:
    for entry in source:
        path = pathlib.PurePosixPath(entry.name)
        if path.is_absolute() or '..' in path.parts or path.parts[0] != archive.name[:-7]:
            raise SystemExit('Unsafe toolchain archive member: ' + entry.name)
        if entry.isdev() or entry.isfifo():
            raise SystemExit('Unexpected toolchain archive device')
        if entry.issym() or entry.islnk():
            base = path.parent if entry.issym() else pathlib.PurePosixPath('.')
            target = pathlib.PurePosixPath(posixpath.normpath(str(base / entry.linkname)))
            if target.is_absolute() or '..' in target.parts or target.parts[0] != archive.name[:-7]:
                raise SystemExit('Toolchain link escapes extraction directory')
PY
tar -xJf "$archive" --no-same-owner -C toolchains
touch toolchains/ky-toolchain-linux-glibc-x86_64-v1.0.1/.download-complete
test -x toolchains/ky-toolchain-linux-glibc-x86_64-v1.0.1/bin/riscv64-unknown-linux-gnu-gcc

ccache --max-size=2G
ccache --zero-stats
touch _ci/state/ccache.ready
if compgen -G 'external/cache/rootfs/*.tar.lz4' > /dev/null; then
    bash ci/r2s/cache.sh rootfs-check
fi

# Explicitly require RISC-V binfmt support before the foreign chroot stages.
mountpoint -q /proc/sys/fs/binfmt_misc || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
update-binfmts --enable qemu-riscv64
grep -q '^enabled' /proc/sys/fs/binfmt_misc/qemu-riscv64
# Final images do not contain QEMU: the kernel must hold its interpreter open.
grep -q '^flags:.*F' /proc/sys/fs/binfmt_misc/qemu-riscv64
losetup --find > /dev/null
available=$(df -PB1 "$root" | awk 'NR == 2 {print $4}')
(( available >= 10 * 1024 * 1024 * 1024 )) || {
    printf 'Need at least 10 GiB free before building, have %s bytes.\n' "$available" >&2
    exit 1
}
cp ci/r2s/config.conf userpatches/config-r2s-debian.conf
touch .ignore_changes
start=$(date +%s)
# Upstream intentionally does not run with errexit; fail on its result AND on
# the independent image validator, not merely on an apparent successful exit.
bash build.sh r2s-debian
test -s _ci/state/image.verified
printf '%s\n' "$(( $(date +%s) - start ))" > _ci/state/build-seconds
ccache --show-stats > _ci/logs/ccache.txt
gzip_files=(output/images/*/*.img.gz)
[[ ${#gzip_files[@]} == 1 && -s ${gzip_files[0]} ]] || {
    printf '%s\n' 'Expected exactly one compressed R2S image.' >&2
    exit 1
}
gzip -t "${gzip_files[0]}"
image_dir=$(dirname "${gzip_files[0]}")
(cd "$image_dir" && sha256sum -c ./*.img.gz.sha)
# Verify that compression still represents the validated raw image.
actual=$(gzip -dc "${gzip_files[0]}" | sha256sum | cut -d' ' -f1)
expected=$(cut -d' ' -f1 _ci/state/raw-image.sha256)
[[ $actual == "$expected" ]]
mkdir -p _ci/release/{locks,logs,packages}
cp "${gzip_files[0]}" "${gzip_files[0]}.sha" _ci/release/
cp _ci/state/raw-image.sha256 _ci/release/
cp _ci/state/*.json _ci/state/build.env _ci/state/source-date-epoch _ci/release/locks/
cp _ci/logs/* _ci/release/logs/
cp output/debug/*.log _ci/release/logs/
cp -r output/config _ci/release/
cp -r output/debs/. _ci/release/packages/
mkdir -p _ci/release/{boot,bootloader}
cp _ci/state/boot/* _ci/release/boot/
cp "u-boot/$R2S_UBOOT_COMMIT"/{bootinfo_sd.bin,bootinfo_emmc.bin,FSBL.bin,u-boot-env-default.bin,u-boot-opensbi.itb} _ci/release/bootloader/
(cd _ci/release && find . -type f ! -name sha256sums -print0 | sort -z | xargs -0 sha256sum > sha256sums)
touch _ci/state/release.ready
