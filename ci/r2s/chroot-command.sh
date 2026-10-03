#!/bin/sh
# Executable equivalent of chroot-env.sh for non-Bash bootstrap programs.
set -eu
target=$1
shift
exec /usr/sbin/chroot "$target" /usr/bin/env TMPDIR=/tmp HOME=/root "$@"
