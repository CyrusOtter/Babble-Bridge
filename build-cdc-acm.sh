#!/usr/bin/env bash
# Builds cdc-acm.ko for the kernel running on a Steam Frame.
#
# The stock Steam Frame kernel ships without the USB CDC-ACM (serial) class driver, so a Babble
# tracker enumerates on the rear USB-C port but no /dev/ttyACM0 appears. This script builds the
# driver out of tree, on the headset, without root and without touching the read-only rootfs:
#
#   1. the matching linux-*-deckard-headers package is fetched with pacman from the repositories
#      already configured on the headset (their URLs are private and are never printed here)
#      and unpacked under ~/kbuild;
#   2. drivers/usb/class/cdc-acm.{c,h} of the same upstream kernel version are downloaded from
#      kernel.org (GitHub as fallback) into ~/cdc-acm;
#   3. make builds cdc-acm.ko against the headers. BTF generation is skipped (no vmlinux in the
#      headers package); the module loads fine without it.
#
# Result: ~/cdc-acm/cdc-acm.ko, the path 90-babble-tracker.rules loads on plug-in.
# Rebuild after every OS update (the kernel version changes; the script tells you when the
# module no longer matches).
#
#   bash build-cdc-acm.sh              build for the running kernel
#   bash build-cdc-acm.sh --load       ...and insmod it now (asks for the sudo password)
#   bash build-cdc-acm.sh --check      only report whether ~/cdc-acm/cdc-acm.ko matches the kernel
#   bash build-cdc-acm.sh --source DIR use cdc-acm.c/.h from DIR instead of downloading
#   bash build-cdc-acm.sh --refresh    re-download headers and sources even if present
#
# Environment: WORK (default ~/kbuild), OUT (default ~/cdc-acm).
set -euo pipefail

WORK="${WORK:-$HOME/kbuild}"
OUT="${OUT:-$HOME/cdc-acm}"
LOAD=0
CHECK=0
REFRESH=0
SOURCE_DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --load) LOAD=1 ;;
        --check) CHECK=1 ;;
        --refresh) REFRESH=1 ;;
        --source) SOURCE_DIR="$2"; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }

KVER="$(uname -r)"                                   # 6.18.0-gfbdbca41fd45
VERMAGIC_PREFIX="$KVER "

module_matches() {
    [ -f "$OUT/cdc-acm.ko" ] || return 1
    modinfo "$OUT/cdc-acm.ko" 2>/dev/null | grep -q "^vermagic: *${VERMAGIC_PREFIX}"
}

if [ "$CHECK" = 1 ]; then
    if module_matches; then
        echo "$OUT/cdc-acm.ko matches the running kernel $KVER"
        exit 0
    fi
    echo "$OUT/cdc-acm.ko is missing or was built for another kernel (running: $KVER); rebuild it"
    exit 1
fi

for tool in gcc make pacman curl bsdtar modinfo; do
    command -v "$tool" >/dev/null || die "$tool is not available on this headset"
done

# --- kernel package -----------------------------------------------------------------------------

KPKG="$(pacman -Qq 2>/dev/null | grep -E '^linux-[0-9]+-deckard$' | head -1 || true)"
[ -n "$KPKG" ] || die "no linux-*-deckard kernel package found; is this a Steam Frame?"
KPKGVER="$(pacman -Q "$KPKG" | awk '{print $2}')"   # 6.18.0+gfbdbca41fd45-1
HDRPKG="${KPKG}-headers"
BUILD_DIR="$WORK/usr/lib/modules/$KVER/build"

# Upstream tag holding the same driver sources: 6.18.0-g... -> v6.18, 6.18.3-g... -> v6.18.3
BASE="${KVER%%-*}"
case "$BASE" in
    *.*.0) UPSTREAM="v${BASE%.0}" ;;
    *) UPSTREAM="v$BASE" ;;
esac

say "kernel $KVER ($KPKG $KPKGVER), driver sources from upstream $UPSTREAM"

# --- headers -------------------------------------------------------------------------------------

if [ "$REFRESH" = 1 ] || [ ! -f "$BUILD_DIR/Makefile" ]; then
    mkdir -p "$WORK"
    say "fetching $HDRPKG through pacman"
    # pacman -Sp lists the download URL(s) of the package and its uncached dependencies; keep
    # only the headers package and never print the URL (the repositories are private).
    URL="$(pacman -Sp "$HDRPKG" 2>/dev/null | grep -F "/${HDRPKG}-" | head -1 || true)"
    [ -n "$URL" ] || die "pacman does not know $HDRPKG (repository not configured or offline)"
    PKGFILE="$WORK/$(basename "$URL")"
    PKGVER_IN_REPO="$(basename "$URL" | sed -E "s/^${HDRPKG}-(.*)-[a-z0-9_]+\.pkg\.tar\.[a-z]+$/\1/")"
    if [ "$PKGVER_IN_REPO" != "$KPKGVER" ]; then
        die "the repository offers $HDRPKG $PKGVER_IN_REPO but the running kernel is $KPKGVER; a module built now would not load. Update the headset (or wait for the matching headers) and retry."
    fi
    if [ "$REFRESH" = 1 ] || [ ! -s "$PKGFILE" ]; then
        echo "downloading $(basename "$PKGFILE")"
        curl -fsSL -o "$PKGFILE" "$URL"
    fi
    echo "unpacking into $WORK"
    bsdtar -xf "$PKGFILE" -C "$WORK"
    [ -f "$BUILD_DIR/Makefile" ] || die "headers did not provide $BUILD_DIR"
else
    echo "using headers in $BUILD_DIR"
fi

# The headers package ships a vmlinux, which makes the kernel build system run pahole to attach
# BTF to the module. pahole is not on the headset (and cannot be installed without root), so set
# vmlinux aside in our private copy: the build then prints "Skipping BTF generation" and the
# module loads without BTF. Restore it if pahole is ever available.
if [ -f "$BUILD_DIR/vmlinux" ] && ! command -v pahole >/dev/null; then
    mv "$BUILD_DIR/vmlinux" "$BUILD_DIR/vmlinux.no-pahole"
    echo "no pahole on this headset: building without BTF (vmlinux set aside)"
fi

# --- driver sources ------------------------------------------------------------------------------

mkdir -p "$OUT"
fetch_source() {
    local name="$1" dest="$OUT/$1"
    if [ -n "$SOURCE_DIR" ]; then
        cp "$SOURCE_DIR/$name" "$dest"
        return
    fi
    if [ "$REFRESH" = 0 ] && [ -s "$dest" ]; then
        echo "using existing $dest"
        return
    fi
    local urls=(
        "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/plain/drivers/usb/class/$name?h=$UPSTREAM"
        "https://raw.githubusercontent.com/torvalds/linux/$UPSTREAM/drivers/usb/class/$name"
    )
    local url
    for url in "${urls[@]}"; do
        echo "downloading $name ($UPSTREAM) from $(echo "$url" | awk -F/ '{print $3}')"
        if curl -fsSL -o "$dest.tmp" "$url" && head -c 40 "$dest.tmp" | grep -q "SPDX-License-Identifier"; then
            mv "$dest.tmp" "$dest"
            return
        fi
        rm -f "$dest.tmp"
    done
    die "could not download $name for $UPSTREAM; fetch drivers/usb/class/$name from a kernel tree on another machine and pass --source DIR"
}
fetch_source cdc-acm.c
fetch_source cdc-acm.h
printf 'obj-m := cdc-acm.o\n' > "$OUT/Makefile"

# --- build ---------------------------------------------------------------------------------------

say "building cdc-acm.ko"
make -C "$BUILD_DIR" M="$OUT" modules 2>&1 | tee "$OUT/build.log" | grep -v "^make\[1\]"
module_matches || die "the built module's vermagic does not match $KVER (see $OUT/build.log)"
say "built $OUT/cdc-acm.ko ($(stat -c %s "$OUT/cdc-acm.ko") bytes, vermagic $KVER)"
grep -q "Skipping BTF" "$OUT/build.log" && echo "note: built without BTF (no vmlinux in the headers package); that is fine for loading"

# --- load ----------------------------------------------------------------------------------------

if [ "$LOAD" = 1 ]; then
    if lsmod | grep -q '^cdc_acm '; then
        echo "cdc_acm is already loaded"
    else
        say "loading it now (sudo)"
        sudo insmod "$OUT/cdc-acm.ko"
        sleep 1
    fi
    ls -la /dev/ttyACM* 2>/dev/null || echo "no /dev/ttyACM* yet: plug the tracker in (or re-plug it)"
else
    cat <<MSG

To load it now:                     sudo insmod $OUT/cdc-acm.ko
To load it automatically on plug-in (once, as root, from the Babble-Bridge checkout):
    sudo install -m 0644 90-babble-tracker.rules /etc/udev/rules.d/ && sudo udevadm control --reload
MSG
fi
