#!/bin/bash
# One-time root setup for the Babble tracker's cdc-acm driver on a Steam Frame: the module built
# by build-cdc-acm.sh is loaded at every boot of the kernel it was built for, without touching the
# read-only rootfs (nothing under /usr or /lib/modules, no steamos-readonly disable).
#
# Normally you do not run this by hand: `bash build-cdc-acm.sh --install` (in the same Babble-Bridge
# copy) builds the module and runs this with sudo (one password). By hand, on the headset, in a
# terminal (ssh -t) so sudo can ask for the password:
#
#   sudo /usr/bin/bash root/install-cdc-acm-loader.sh --headers "$(cat ~/cdc-acm/cdc-acm.headers)" < ~/cdc-acm/cdc-acm.ko
#   sudo /usr/bin/bash root/install-cdc-acm-loader.sh               install or update the files only
#   sudo /usr/bin/bash root/install-cdc-acm-loader.sh --uninstall   remove everything again
#
# --headers hands the module to stage-cdc-acm, which asks before it loads it (--yes: do not ask).
# An armed unit only ever runs files that have loaded the module once through the unit. So when an
# update changes the boot path (load-cdc-acm or the unit file) of a unit that is armed, or may be
# (systemctl gives no answer, or a wants link is left without its unit file), this disarms the unit
# before it replaces anything and ends with stage-cdc-acm (--rearm, or the --headers staging),
# which arms it again only after the new files have loaded the module. If cdc_acm cannot be
# unloaded for that test (a process holds the tracker port), or the disarm cannot be confirmed, it
# stops before it replaces anything.
#
# Read root/ first: load-cdc-acm runs as root at every boot, stage-cdc-acm whenever you stage.
# Installs, all root:root:
#   /var/lib/babble-tracker/load-cdc-acm, stage-cdc-acm  0755  copied to the new slot on OS updates
#   /etc/systemd/system/babble-cdc-acm.service           0644  on the default atomic-update keep list
#   /etc/udev/rules.d/70-babble-tracker.rules            0644  kept by the drop-in below
#   /etc/atomic-update.conf.d/babble-tracker.conf        0644  keep-list drop-in
# and removes the old /etc/udev/rules.d/90-babble-tracker.rules (it ran insmod on a file in /home).
# The unit is never enabled here: only stage-cdc-acm enables it, once the module has loaded.
set -euo pipefail
export PATH=/usr/bin LC_ALL=C
umask 022

STORE=/var/lib/babble-tracker
UNIT=babble-cdc-acm.service
UNIT_FILE=/etc/systemd/system/$UNIT
WANTS=/etc/systemd/system/multi-user.target.wants/$UNIT   # the link that arms the unit (WantedBy=)
RULE=/etc/udev/rules.d/70-babble-tracker.rules
KEEP_DIR=/etc/atomic-update.conf.d
KEEP=$KEEP_DIR/babble-tracker.conf
OLD_RULE=/etc/udev/rules.d/90-babble-tracker.rules
FREE_PORT="unplug the tracker, or stop the FCAM overlay that holds its port with python3 ~/fcam/fcam_overlay.py --stop (it refuses an instance that may use raw mode, which must not be stopped: then unplug the tracker) and start it again afterwards with python3 ~/fcam/fcam_overlay.py --start"

die() { echo "error: $*" >&2; exit 1; }
# What systemctl says about the unit: enabled (armed for boot), disabled, ... or nothing at all
# when it cannot tell (which never counts as disarmed).
unit_enabled() { systemctl is-enabled "$UNIT" </dev/null 2>/dev/null || true; }
# absent, live, builtin (part of the kernel: no initstate), or coming/going: a load or unload that
# never finished (as in stage-cdc-acm).
module_state() {
    local state
    if [ ! -d /sys/module/cdc_acm ]; then
        echo absent
        return
    fi
    state="$(cat /sys/module/cdc_acm/initstate 2>/dev/null)" || state=builtin
    echo "${state:-unknown}"
}

ACTION=install
HEADERS=""
YES=""
while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) ACTION=uninstall ;;
        --headers) [ $# -ge 2 ] || die "--headers needs a value"; HEADERS="$2"; shift ;;
        --yes) YES=--yes ;;
        -h|--help) sed -n '2,/^[^#]/{/^#/p}' "$0"; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
    shift
done
[ "$(id -u)" = 0 ] || die "run it with sudo: sudo /usr/bin/bash $0"
[ "$ACTION" = install ] || [ -z "$HEADERS" ] || die "--headers and --uninstall do not go together"
[ -n "$HEADERS" ] || [ -z "$YES" ] || die "--yes only goes with --headers"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
build="$(dirname "$here")/build-cdc-acm.sh"

if [ "$ACTION" = uninstall ]; then
    systemctl disable --quiet "$UNIT" </dev/null 2>/dev/null || true
    # Stopping only drops the unit's state; the driver stays loaded (no ExecStop).
    systemctl stop "$UNIT" </dev/null 2>/dev/null || true
    rm -f -- "$UNIT_FILE" "$WANTS" "$RULE" "$KEEP" "$OLD_RULE"
    rm -rf -- "$UNIT_FILE.d" "$STORE"
    systemctl daemon-reload </dev/null
    systemctl reset-failed "$UNIT" </dev/null 2>/dev/null || true
    udevadm control --reload </dev/null
    # A port that exists now keeps what the rule gave it (/dev/babble-tracker, the uaccess tag logind
    # grants the seat ACL from) until udev processes it again without the rule.
    udevadm trigger --action=change --subsystem-match=tty --sysname-match='ttyACM*' </dev/null || true
    echo "removed $UNIT, $RULE, $KEEP and $STORE"
    if [ -d /sys/module/cdc_acm ]; then
        echo "cdc_acm stays loaded until the next reboot (or unplug the tracker and: sudo rmmod cdc_acm)"
        echo "a tracker port that exists now keeps the access it was already granted until it is unplugged or the headset reboots"
    fi
    exit 0
fi

for f in load-cdc-acm stage-cdc-acm babble-cdc-acm.service 70-babble-tracker.rules babble-tracker.atomic-update.conf; do
    { [ -f "$here/$f" ] && [ ! -L "$here/$f" ]; } ||
        die "$here/$f is missing; run this from a Babble-Bridge checkout or release"
    if grep -q $'\r' "$here/$f"; then
        die "$here/$f has Windows (CRLF) line endings; check the repository out with LF (.gitattributes)"
    fi
done
[ -z "$HEADERS" ] || [ ! -t 0 ] || die "--headers needs the module on standard input: ... < ~/cdc-acm/cdc-acm.ko"

# Before the copy: is the unit armed, and does the boot path (what runs at boot: the loader and the
# unit) change? An armed unit must not start running untested files at the next boot.
same_file() { [ -f "$2" ] && [ "$(sha256sum < "$1")" = "$(sha256sum < "$2")" ]; }
# Armed unless it is known not to be: a wants link (also one whose unit file is gone: installing
# the unit file makes it count again), or an installed unit that systemctl does not positively call
# disabled (enabled, something else, or no answer at all, e.g. a D-Bus timeout). Only a first
# install (no unit file, no link) or "disabled" counts as not armed.
unit_present=0
if [ -e "$UNIT_FILE" ] || [ -L "$UNIT_FILE" ]; then
    unit_present=1
fi
was_state="$(unit_enabled)"
was_enabled=0
if [ -e "$WANTS" ] || [ -L "$WANTS" ]; then
    was_enabled=1
elif [ "$unit_present" = 1 ] && [ "$was_state" != disabled ]; then
    was_enabled=1
fi
boot_changed=0
{ same_file "$here/load-cdc-acm" "$STORE/load-cdc-acm" && same_file "$here/babble-cdc-acm.service" "$UNIT_FILE"; } ||
    boot_changed=1
# The installed files this run adds or replaces (reported when the staging after the copy stops).
changed=()
for pair in load-cdc-acm:"$STORE/load-cdc-acm" stage-cdc-acm:"$STORE/stage-cdc-acm" \
            babble-cdc-acm.service:"$UNIT_FILE" 70-babble-tracker.rules:"$RULE" \
            babble-tracker.atomic-update.conf:"$KEEP"; do
    same_file "$here/${pair%%:*}" "${pair#*:}" || changed+=("${pair#*:}")
done
KVER="$(uname -r)"
staged="$STORE/by-kernel/$KVER/cdc-acm.ko"
# A module staged on exactly the running kernel build: the only one the unit loads. After a rebuild
# of the same kernel release (new uname -v) the copy staged before is skipped at boot, and --rearm
# cannot help: the module has to be built and staged again.
staged_for_this_build() {
    [ -f "$staged" ] && [ "$(cat -- "$STORE/by-kernel/$KVER/kernel-build" 2>/dev/null)" = "$(uname -v)" ]
}

# What runs after the copy: the staging with --headers, or, when an armed unit's boot path changes,
# a test of the new files with the module already staged.
stage_args=()
if [ -n "$HEADERS" ]; then
    stage_args=(--headers "$HEADERS")
    [ -z "$YES" ] || stage_args+=("$YES")
elif [ "$was_enabled" = 1 ] && [ "$boot_changed" = 1 ] && staged_for_this_build; then
    stage_args=(--rearm)
fi

disarmed=0
if [ "$was_enabled" = 1 ] && [ "$boot_changed" = 1 ]; then
    # The test needs cdc_acm unloaded. What stage-cdc-acm would refuse is checked before anything
    # changes, so that the old, tested files stay armed.
    if [ "${#stage_args[@]}" -gt 0 ]; then
        state="$(module_state)"
        case "$state" in
            absent|live|builtin) ;;
            *) die "cdc_acm is stuck in state '$state' (did a load crash? journalctl -k -b). Nothing was changed; reboot, then run the same command again" ;;
        esac
        refcnt="$(cat /sys/module/cdc_acm/refcnt 2>/dev/null || echo 0)"
        if [ "$state" = live ] && [ "$refcnt" != 0 ]; then
            die "the boot path (load-cdc-acm, $UNIT) changes and $UNIT is armed (systemctl is-enabled says '${was_state:-nothing}'), but cdc_acm is in use (refcnt $refcnt) and cannot be unloaded to test the new files. Nothing was changed: the installed files and $UNIT stay as they were. To go ahead, $FREE_PORT, then run the same command again"
        fi
    fi
    # Disarmed before any file changes; stage-cdc-acm arms the unit again once the new files have
    # loaded the module through it. The link goes too where systemctl cannot remove it (no unit
    # file, or no answer). Only a confirmed disarm goes on: no link, and "disabled" (or "not-found"
    # when there was no unit file at all); anything else stops here, before a file is replaced.
    systemctl disable --quiet "$UNIT" </dev/null 2>/dev/null || true
    rm -f -- "$WANTS" || true
    state="$(unit_enabled)"
    disarm_ok=0
    if [ ! -e "$WANTS" ] && [ ! -L "$WANTS" ]; then
        case "$state" in
            disabled) disarm_ok=1 ;;
            not-found) [ "$unit_present" = 1 ] || disarm_ok=1 ;;
        esac
    fi
    [ "$disarm_ok" = 1 ] ||
        die "cannot confirm that $UNIT is disarmed (systemctl is-enabled says '${state:-nothing}'). The loader files were not replaced, but $UNIT may or may not still be armed for boot now (check: systemctl is-enabled $UNIT); run the same command again once systemctl answers"
    disarmed=1
    echo "the boot path changes (load-cdc-acm or $UNIT): $UNIT is disarmed until the new files have loaded the module once"
fi

# Nothing below reads standard input: with --headers it still holds the module for stage-cdc-acm.
install -d -o root -g root -m 0755 "$STORE" "$STORE/by-kernel"
install -o root -g root -m 0755 "$here/load-cdc-acm" "$STORE/load-cdc-acm"
install -o root -g root -m 0755 "$here/stage-cdc-acm" "$STORE/stage-cdc-acm"
install -o root -g root -m 0644 "$here/babble-cdc-acm.service" "$UNIT_FILE"
install -o root -g root -m 0644 "$here/70-babble-tracker.rules" "$RULE"
# The directory ships with SteamOS; create it only if it is missing, never change it.
[ -d "$KEEP_DIR" ] || install -d -o root -g root -m 0755 "$KEEP_DIR"
install -o root -g root -m 0644 "$here/babble-tracker.atomic-update.conf" "$KEEP"
removed_old=""
if [ -e "$OLD_RULE" ] || [ -L "$OLD_RULE" ]; then
    rm -f -- "$OLD_RULE"
    removed_old=1
    echo "removed $OLD_RULE (it ran insmod on a user-writable file in /home)"
fi
systemctl daemon-reload </dev/null
udevadm control --reload </dev/null
# Apply the tty rule (uaccess ACL, /dev/babble-tracker) to a port that already exists.
udevadm trigger --action=change --subsystem-match=tty --sysname-match='ttyACM*' </dev/null || true
echo "installed the cdc-acm loader:"
sha256sum "$STORE/load-cdc-acm" "$STORE/stage-cdc-acm" "$UNIT_FILE" "$RULE" "$KEEP"

if [ "${#stage_args[@]}" -gt 0 ]; then
    if [ "${stage_args[0]}" = --rearm ]; then
        echo "loading the module staged for $KVER once through the new files (stage-cdc-acm --rearm)"
    fi
    rc=0
    "$STORE/stage-cdc-acm" "${stage_args[@]}" || rc=$?
    if [ "$rc" != 0 ]; then
        # What stage-cdc-acm reports covers the staged build and the unit; this run changed more.
        done_here=""
        if [ "${#changed[@]}" -gt 0 ]; then
            done_here="installed ${changed[*]}"
        fi
        if [ -n "$removed_old" ]; then
            done_here="${done_here:+$done_here and }removed $OLD_RULE"
        fi
        if [ -n "$done_here" ]; then
            echo "note: staging stopped, but this run already $done_here" >&2
        fi
    fi
    if [ "$rc" != 0 ] && [ "$disarmed" = 1 ] && [ "$(unit_enabled)" != enabled ] && [ "$(module_state)" != builtin ]; then
        if staged_for_this_build; then
            next="sudo $STORE/stage-cdc-acm --rearm   (loads the module staged for $KVER through them and arms it)"
        else
            next="as steamos: bash $build --install"
        fi
        echo "The new loader files are installed but not verified yet, so $UNIT stays disarmed ($(unit_enabled)) and the next boot does not load cdc_acm. To test and arm them: $next" >&2
    fi
    exit "$rc"
fi
if [ "$disarmed" = 1 ]; then
    if [ -f "$staged" ]; then
        echo "the module staged for $KVER belongs to another build of that kernel, so $UNIT stays disarmed ($(unit_enabled))"
    else
        echo "nothing is staged for kernel $KVER, so $UNIT stays disarmed ($(unit_enabled))"
    fi
elif [ "$was_enabled" = 1 ] && staged_for_this_build; then
    if [ "$was_state" = enabled ]; then
        echo "the boot path is unchanged: the module staged for $KVER and $UNIT stay armed"
    else
        echo "the boot path is unchanged, so $UNIT was left as it was (systemctl is-enabled says '${was_state:-nothing}'; check: systemctl is-enabled $UNIT)"
    fi
    exit 0
fi
if staged_for_this_build; then
    echo "next: sudo $STORE/stage-cdc-acm --rearm   (loads the module staged for $KVER and arms it for boot)"
else
    echo "next, as steamos: bash $build --install   (builds and stages the module, loads it and arms it for boot)"
fi
