#!/usr/bin/env bash
# Builds cdc-acm.ko for the kernel running on a Steam Frame and, with --install, has it loaded at
# every boot of that kernel.
#
# The stock Steam Frame kernel ships without the USB CDC-ACM (serial) class driver, so a Babble
# tracker enumerates on the rear USB-C port but no /dev/ttyACM0 appears. This script builds the
# driver out of tree, on the headset, without root and without touching the read-only rootfs:
#
#   1. the matching linux-*-deckard-headers package is fetched with pacman from the repositories
#      already configured on the headset (their URLs are private and are never printed here),
#      checked against the sha256 in the headset's own package database and unpacked under
#      ~/kbuild, again whenever the installed kernel package changes;
#   2. drivers/usb/class/cdc-acm.{c,h} of the same upstream kernel version are downloaded from
#      kernel.org (GitHub as fallback) into ~/cdc-acm, again whenever that version changes;
#   3. make builds cdc-acm.ko against the headers, from clean. BTF generation is skipped (no
#      pahole on the headset); the module loads fine without it.
#
# Result: ~/cdc-acm/cdc-acm.ko and ~/cdc-acm/cdc-acm.headers (the headers release it was built
# against). Nothing is ever loaded from there: --install hands the module to the root-owned
# loader (root/, one sudo password), which keeps a checked copy in /var/lib/babble-tracker and
# loads it at every boot. Run --install again after an OS update that changes the kernel;
# --check tells you when. The udev rule of 0.1.x did load that file as root; while it is still
# installed, every mode warns, --check fails, a plain build refuses, and --install removes it.
#
#   bash build-cdc-acm.sh              build for the running kernel (no root)
#   bash build-cdc-acm.sh --install    ...then stage it for loading at boot and load it now (sudo)
#   bash build-cdc-acm.sh --check      only report: build, staged copy, boot unit, driver loaded
#   bash build-cdc-acm.sh --source DIR use cdc-acm.c/.h from DIR instead of downloading
#   bash build-cdc-acm.sh --refresh    re-download headers and sources even if present
#
# Run it as steamos, not with sudo. Over ssh, use a terminal (ssh -t) so sudo can ask for the
# password. Environment: WORK (default ~/kbuild), OUT (default ~/cdc-acm).
set -euo pipefail

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${WORK:-$HOME/kbuild}"
OUT="${OUT:-$HOME/cdc-acm}"
STORE=/var/lib/babble-tracker                     # root-owned loader and module store (root/)
UNIT=babble-cdc-acm.service
OLD_RULE=/etc/udev/rules.d/90-babble-tracker.rules   # 0.1.x: root loaded ~/cdc-acm/cdc-acm.ko on every plug-in
INSTALL=0
CHECK=0
REFRESH=0
SOURCE_DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --install) INSTALL=1 ;;
        --load) INSTALL=1; echo "note: --load is now --install (the module is staged and loaded at boot, never insmod-ed from $OUT)" ;;
        --check) CHECK=1 ;;
        --refresh) REFRESH=1 ;;
        --source) [ $# -ge 2 ] || die "--source needs a directory"; SOURCE_DIR="$2"; shift ;;
        -h|--help) sed -n '2,/^[^#]/{/^#/p}' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

KVER="$(uname -r)"                                   # 6.18.0-gfbdbca41fd45

# The package that ships the running kernel and its version: the headers must be this release.
KPKG="$(cat "/usr/lib/modules/$KVER/pkgbase" 2>/dev/null || true)"          # linux-618-deckard
[ -n "$KPKG" ] || KPKG="$(pacman -Qqo "/usr/lib/modules/$KVER/modules.order" 2>/dev/null || true)"
[ -n "$KPKG" ] || KPKG="$(pacman -Qq 2>/dev/null | grep -E '^linux-[0-9]+-deckard$' | head -1 || true)"
KPKGVER=""
[ -z "$KPKG" ] || KPKGVER="$(pacman -Q "$KPKG" 2>/dev/null | awk '{print $2}' || true)"   # 6.18.0+gfbdbca41fd45-1

vermagic_matches() {   # first vermagic field is the running kernel release
    local magic
    [ -f "$OUT/cdc-acm.ko" ] || return 1
    magic="$(modinfo -F vermagic "$OUT/cdc-acm.ko" 2>/dev/null)" || return 1
    [ "${magic%% *}" = "$KVER" ]
}
build_matches() {   # vermagic of the running kernel, built against the installed kernel's headers
    vermagic_matches && [ -n "$KPKGVER" ] && [ "$(cat "$OUT/cdc-acm.headers" 2>/dev/null)" = "$KPKGVER" ]
}
module_state() {   # absent, live, builtin, or coming/going (a load that never finished: its init crashed?)
    local state
    if [ ! -d /sys/module/cdc_acm ]; then
        echo absent
        return
    fi
    state="$(cat /sys/module/cdc_acm/initstate 2>/dev/null)" || state=builtin
    echo "${state:-unknown}"
}
old_rule_present() { [ -e "$OLD_RULE" ] || [ -L "$OLD_RULE" ]; }
warn_old_rule() {
    cat >&2 <<MSG
WARNING: $OLD_RULE from Babble-Bridge 0.1.x is still installed.
  It has root load ~/cdc-acm/cdc-acm.ko whenever the tracker (or any ESP32 board) is plugged in,
  and any program running as steamos can replace that file. Remove it (one sudo password, ssh -t):
    sudo /usr/bin/bash $here/root/install-cdc-acm-loader.sh   (installs the new loader, removes the rule)
  or only remove it:
    sudo rm $OLD_RULE && sudo udevadm control --reload
MSG
}
helpers_outdated() {   # the files in root/ of this checkout differ from the installed ones
    local src dst
    for src in load-cdc-acm:"$STORE/load-cdc-acm" \
               stage-cdc-acm:"$STORE/stage-cdc-acm" \
               babble-cdc-acm.service:"/etc/systemd/system/$UNIT" \
               70-babble-tracker.rules:/etc/udev/rules.d/70-babble-tracker.rules \
               babble-tracker.atomic-update.conf:/etc/atomic-update.conf.d/babble-tracker.conf; do
        dst="${src#*:}"
        src="$here/root/${src%%:*}"
        { [ -f "$src" ] && [ -f "$dst" ]; } || continue
        [ "$(sha256sum < "$src")" = "$(sha256sum < "$dst")" ] || return 0
    done
    return 1
}

if old_rule_present; then
    warn_old_rule
fi

if [ "$CHECK" = 1 ]; then
    rc=0
    if old_rule_present; then
        echo "old:     $OLD_RULE (0.1.x) is installed and loads a file in /home as root (see the warning above)"
        rc=1
    fi
    if build_matches; then
        echo "build:   $OUT/cdc-acm.ko fits the running kernel $KVER ($KPKG $KPKGVER)"
    else
        echo "build:   $OUT/cdc-acm.ko is missing or not built for the running kernel $KVER (${KPKG:-?} ${KPKGVER:-?})"
        rc=1
    fi
    staged="$STORE/by-kernel/$KVER"
    if [ ! -x "$STORE/load-cdc-acm" ]; then
        echo "loader:  not installed"
        rc=1
    elif [ ! -f "$staged/cdc-acm.ko" ]; then
        echo "staged:  nothing for kernel $KVER"
        rc=1
    elif [ "$(cat "$staged/kernel-build" 2>/dev/null)" != "$(uname -v)" ]; then
        echo "staged:  $staged/cdc-acm.ko belongs to another build of $KVER"
        rc=1
    else
        echo "staged:  $staged/cdc-acm.ko (sha256 $(cut -c1-16 "$staged/cdc-acm.ko.sha256")...)"
    fi
    enabled="$(systemctl is-enabled "$UNIT" 2>/dev/null || true)"
    active="$(systemctl is-active "$UNIT" 2>/dev/null || true)"
    echo "boot:    $UNIT ${enabled:-not installed}, ${active:-?}"
    [ "$enabled" = enabled ] || rc=1
    state="$(module_state)"
    case "$state" in
        live|builtin) echo "driver:  cdc_acm loaded" ;;
        absent) echo "driver:  cdc_acm not loaded"; rc=1 ;;
        *) echo "driver:  cdc_acm stuck in state '$state' (did loading it crash? journalctl -k -b); reboot"; rc=1 ;;
    esac
    if helpers_outdated; then
        echo "note:    root/ in $here differs from the installed loader; update it: sudo /usr/bin/bash $here/root/install-cdc-acm-loader.sh"
    fi
    [ "$rc" = 0 ] || echo "fix:     bash $here/build-cdc-acm.sh --install   (one sudo password; add --refresh after a same-release kernel rebuild)"
    exit "$rc"
fi

# The build runs the headers package's prebuilt tools and writes into $HOME: never as root. The
# only root step is the sudo call at the end, which hands the finished module over on stdin.
[ "$(id -u)" != 0 ] || die "run this as steamos, not as root or with sudo (--install asks for the password itself)"
# The old rule would load the new $OUT/cdc-acm.ko as root at the next plug-in; --install removes it
# (through the loader's installer) right after the build.
if old_rule_present && [ "$INSTALL" = 0 ]; then
    die "not building while $OLD_RULE is installed (see the warning above): remove it, or build with --install, which removes it"
fi

for tool in gcc make pacman curl bsdtar modinfo sha256sum; do
    command -v "$tool" >/dev/null || die "$tool is not available on this headset"
done

# --- kernel package -----------------------------------------------------------------------------

[ -n "$KPKG" ] || die "no linux-*-deckard kernel package found; is this a Steam Frame?"
[ -n "$KPKGVER" ] || die "pacman does not list $KPKG"
HDRPKG="${KPKG}-headers"
BUILD_DIR="$WORK/usr/lib/modules/$KVER/build"
HDRSTAMP="$WORK/.headers-$KVER"                     # headers release unpacked into $WORK for $KVER

# Upstream tag holding the same driver sources: 6.18.0-g... -> v6.18, 6.18.3-g... -> v6.18.3
BASE="${KVER%%-*}"
case "$BASE" in
    *.*.0) UPSTREAM="v${BASE%.0}" ;;
    *) UPSTREAM="v$BASE" ;;
esac

say "kernel $KVER ($KPKG $KPKGVER), driver sources from upstream $UPSTREAM"

# --- headers -------------------------------------------------------------------------------------

# Unpack again whenever the installed kernel package changes: a rebuild with the same uname -r
# (new pkgrel) must not be built against the old headers.
if [ "$REFRESH" = 1 ] || [ ! -f "$BUILD_DIR/Makefile" ] || [ "$(cat "$HDRSTAMP" 2>/dev/null)" != "$KPKGVER" ]; then
    mkdir -p "$WORK"
    say "fetching $HDRPKG through pacman"
    # pacman -Sp lists the package and its uncached dependencies; keep only the headers package.
    # %l is the download URL (private, never printed), %h the sha256 recorded in the package
    # database on the read-only rootfs; the download is checked against it before it is unpacked.
    INFO="$(pacman -Sp --print-format '%n %v %h %l' "$HDRPKG" 2>/dev/null | awk -v n="$HDRPKG" '$1 == n' | head -1 || true)"
    [ -n "$INFO" ] || die "pacman does not know $HDRPKG (repository not configured or offline)"
    read -r _ PKGVER_IN_REPO PKGSHA URL <<<"$INFO"
    if [ "$PKGVER_IN_REPO" != "$KPKGVER" ]; then
        die "the repository offers $HDRPKG $PKGVER_IN_REPO but the running kernel is $KPKGVER; a module built now would not load. Update the headset (or wait for the matching headers) and retry."
    fi
    [[ "$PKGSHA" =~ ^[0-9a-f]{64}$ ]] || die "pacman's database has no sha256 for $HDRPKG; refusing to use an unverified download"
    [ -n "$URL" ] || die "pacman gave no download location for $HDRPKG"
    PKGFILE="$WORK/$(basename "$URL")"
    if [ "$REFRESH" = 1 ] || [ ! -s "$PKGFILE" ]; then
        echo "downloading $(basename "$PKGFILE")"
        curl -fsSL -o "$PKGFILE.part" "$URL" || die "download of $(basename "$PKGFILE") failed"
        mv -f "$PKGFILE.part" "$PKGFILE"
    fi
    if [ "$(sha256sum "$PKGFILE" | cut -d' ' -f1)" != "$PKGSHA" ]; then
        rm -f "$PKGFILE"
        die "$(basename "$PKGFILE") does not match the sha256 in pacman's database; deleted it, run the script again"
    fi
    echo "sha256 matches pacman's database; unpacking into $WORK"
    rm -rf "$WORK/usr/lib/modules/$KVER" "$HDRSTAMP"
    bsdtar -xf "$PKGFILE" -C "$WORK"
    [ -f "$BUILD_DIR/Makefile" ] || die "headers did not provide $BUILD_DIR"
    echo "$PKGVER_IN_REPO" > "$HDRSTAMP"
else
    echo "using headers in $BUILD_DIR ($HDRPKG $(cat "$HDRSTAMP"))"
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
# Upstream tag the sources in $OUT were fetched for. Sources from before this stamp existed are
# taken as the running kernel's (that is what they were fetched for); use --refresh if not.
if [ -f "$OUT/.source-tag" ]; then
    SOURCE_TAG="$(cat "$OUT/.source-tag")"
else
    SOURCE_TAG="$UPSTREAM"
fi
fetch_source() {
    local name="$1" dest="$OUT/$1"
    if [ -n "$SOURCE_DIR" ]; then
        cp "$SOURCE_DIR/$name" "$dest"
        return
    fi
    if [ "$REFRESH" = 0 ] && [ -s "$dest" ] && [ "$SOURCE_TAG" = "$UPSTREAM" ]; then
        echo "using existing $dest ($UPSTREAM)"
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
echo "$UPSTREAM" > "$OUT/.source-tag"
printf 'obj-m := cdc-acm.o\n' > "$OUT/Makefile"

# Optional pin: cdc-acm-sources.sha256 next to this script, lines "<sha256>  <tag>/<file>", filled
# in on a PC from a kernel tree. Without a line for this tag the hashes are only printed.
PIN="$here/cdc-acm-sources.sha256"
for name in cdc-acm.c cdc-acm.h; do
    sum="$(sha256sum "$OUT/$name" | cut -d' ' -f1)"
    pinned="$(awk -v p="$UPSTREAM/$name" '$2 == p {print $1}' "$PIN" 2>/dev/null || true)"
    if [ -z "$pinned" ]; then
        echo "$name ($UPSTREAM): sha256 $sum (no pin in $(basename "$PIN"))"
    elif [ "$pinned" = "$sum" ]; then
        echo "$name ($UPSTREAM): sha256 matches the pin"
    else
        die "$OUT/$name does not match the sha256 pinned for $UPSTREAM in $PIN; fetch it again with --refresh"
    fi
done

# --- build ---------------------------------------------------------------------------------------

say "building cdc-acm.ko"
rm -f "$OUT/cdc-acm.headers"
# Headers or sources may have changed: no stale objects.
make -C "$BUILD_DIR" M="$OUT" clean >/dev/null || die "make clean failed in $OUT"
# The filter must not turn "no output left" into a failure; make's own status still counts (pipefail).
if ! make -C "$BUILD_DIR" M="$OUT" modules 2>&1 | tee "$OUT/build.log" | { grep -v "^make\[1\]" || true; }; then
    die "the build failed (see $OUT/build.log)"
fi
vermagic_matches || die "the built module's vermagic does not match $KVER (see $OUT/build.log)"
cp "$HDRSTAMP" "$OUT/cdc-acm.headers"              # stage-cdc-acm refuses a build against other headers
say "built $OUT/cdc-acm.ko ($(stat -c %s "$OUT/cdc-acm.ko") bytes, vermagic $KVER, $HDRPKG $(cat "$OUT/cdc-acm.headers"))"
echo "sha256 $(sha256sum "$OUT/cdc-acm.ko" | cut -d' ' -f1)  (--install: stage-cdc-acm prints the sha256 of what it received and asks before loading it; they must match)"
if grep -q "Skipping BTF" "$OUT/build.log"; then
    echo "note: built without BTF (no pahole on the headset); that is fine for loading"
fi

# --- stage for loading at boot -------------------------------------------------------------------

if helpers_outdated; then
    echo "note: root/ in $here differs from the installed loader; to update it: sudo /usr/bin/bash $here/root/install-cdc-acm-loader.sh"
fi
if [ "$INSTALL" = 1 ]; then
    if [ "$(module_state)" = live ] && [ "$(cat /sys/module/cdc_acm/refcnt 2>/dev/null || echo 0)" != 0 ]; then
        echo "note: cdc_acm is in use (the FCAM overlay holds the tracker port), so staging cannot unload it to test the new build and stops before it stages or arms anything. Before you enter the password, unplug the tracker, or stop the overlay with python3 ~/fcam/fcam_overlay.py --stop (it refuses an instance that may use raw mode, which must not be stopped: then unplug the tracker); afterwards start it again: python3 ~/fcam/fcam_overlay.py --start"
    fi
    HV="$(cat "$OUT/cdc-acm.headers")"
    # Absolute paths only: sudo keeps the user's PATH on SteamOS (no secure_path). While the old
    # rule is installed, the loader's installer runs first: it removes the rule.
    if [ -x "$STORE/stage-cdc-acm" ] && ! old_rule_present; then
        say "staging it for loading at boot (sudo asks for your password)"
        exec /usr/bin/sudo "$STORE/stage-cdc-acm" --headers "$HV" < "$OUT/cdc-acm.ko"
    fi
    [ -f "$here/root/install-cdc-acm-loader.sh" ] ||
        die "$here/root/install-cdc-acm-loader.sh is missing; use a complete Babble-Bridge checkout or release"
    say "installing the loader from $here/root and staging the module (sudo asks for your password)"
    exec /usr/bin/sudo /usr/bin/bash "$here/root/install-cdc-acm-loader.sh" --headers "$HV" < "$OUT/cdc-acm.ko"
fi
cat <<MSG

This build is not staged. To load it now and at every boot of this kernel (one sudo password;
the first time this also installs the loader from $here/root, read it first):
    bash $here/build-cdc-acm.sh --install
MSG
