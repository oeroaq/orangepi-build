# Sourced by CI shell entrypoints; inherited by the upstream Bash builder.
# Host build temporaries stay in /openwrt/_ci/tmp. Guest maintainer scripts
# must use paths that exist inside the root filesystem, never host paths.
chroot()
{
    local target=$1
    shift
    command chroot "$target" /usr/bin/env TMPDIR=/tmp HOME=/root "$@"
}
export -f chroot
