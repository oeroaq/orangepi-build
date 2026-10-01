# Ubuntu 22.04 amd64, pinned independently of the floating tag.
FROM ubuntu:22.04@sha256:281c5745f657873d78e5531fc5ba8575f46ab7769b94550ac99543f122679986

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    acl aria2 bc binfmt-support bison btrfs-progs build-essential ca-certificates \
    ccache cpio cryptsetup curl debian-archive-keyring debootstrap \
    device-tree-compiler dialog dosfstools dwarves fakeroot fdisk flex gawk \
    gdisk git gpg imagemagick iproute2 iptables iputils-ping jq kmod libelf-dev libfdt-dev libfl-dev \
    liblz4-tool libncurses-dev libssl-dev libusb-1.0-0-dev linux-base locales \
    lzop mtools nftables p7zip-full parted patchutils pigz pixz pkg-config psmisc pv \
    python2 python3 python3-dev python3-distutils python3-pyelftools \
    python3-setuptools qemu-user-static rsync scons sudo swig \
    systemd-container u-boot-tools udev unzip uuid-dev uuid-runtime wget \
    whiptail xxd xz-utils zip zlib1g-dev zstd \
    && rm -rf /var/lib/apt/lists/*

# Jammy's original bootstrap/keyring predate Debian 13. Verify both upgrades.
RUN curl -fsSL --retry 5 https://deb.debian.org/debian/pool/main/d/debootstrap/debootstrap_1.0.141_all.deb -o /debootstrap.deb \
    && echo '8c02cb8ad712eb67afb0d23fab175c562804cc97eda70b70cb0f3bc2a21732d7  /debootstrap.deb' | sha256sum -c - \
    && curl -fsSL --retry 5 https://deb.debian.org/debian/pool/main/d/debian-archive-keyring/debian-archive-keyring_2025.1_all.deb -o /keyring.deb \
    && echo '9ea7778e443144ca490668737a8ab22dd3e748bb99e805e22ec055abeb3c7fac  /keyring.deb' | sha256sum -c - \
    && dpkg -i /debootstrap.deb /keyring.deb \
    && rm /debootstrap.deb /keyring.deb \
    && locale-gen en_US.UTF-8 \
    && ln -s /usr/bin/python2 /usr/local/bin/python

ENV HOME=/openwrt/_ci/home TMPDIR=/openwrt/_ci/tmp \
    CCACHE_DIR=/openwrt/external/cache/ccache CCACHE_MAXSIZE=2G \
    CCACHE_COMPILERCHECK=content LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 TERM=xterm
WORKDIR /openwrt
