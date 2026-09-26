#!/usr/bin/env bash
# Installs the FCAM bridge as a SteamVR overlay application on a Steam Frame.
# Run ON the headset as the steamos user, from a copy of this repository or a release tarball:
#
#   scp -r Babble-Bridge steamos@<headset>:~/ && ssh steamos@<headset> 'bash ~/Babble-Bridge/overlay/install-overlay.sh'
#
# Copies the files to ~/fcam, registers fcam.vrmanifest with SteamVR, enables auto-launch and
# (re)starts the overlay. Replaces the fcam-bridge systemd user unit if that was enabled (both would
# bind UDP 8555). No root needed; the cdc-acm driver step (build-cdc-acm.sh --install) is separate.
#
# Extra arguments are overlay options; they are recorded in the manifest SteamVR launches it with,
# and a running instance is restarted with them (a raw-mode one only by a reboot). For example:
#
#   bash install-overlay.sh --texture-mode file    # PNG files that SteamVR loads itself, no GL
#   bash install-overlay.sh                        # back to the defaults (auto: GL, file mode if GL fails)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="$HOME/fcam"

mkdir -p "$dest"
install -m 0755 "$here/../fcam_bridge.py" "$here/fcam_overlay.py" "$here/fcam_overlay.sh" "$dest/"
install -m 0644 "$here/openvr_min.py" "$here/gl_texture.py" "$here/fcam_font.py" "$here/icon.png" \
    "$here/fcam.vrmanifest" "$dest/"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if systemctl --user is-enabled fcam-bridge.service >/dev/null 2>&1; then
    echo "disabling the fcam-bridge systemd unit (the overlay replaces it)"
    systemctl --user disable --now fcam-bridge.service
fi
systemctl --user stop fcam-bridge.service 2>/dev/null || true

python3 "$dest/fcam_overlay.py" --install "$@"

mode="$(python3 "$dest/fcam_overlay.py" --status 2>/dev/null | sed -n 's/^texture mode: //p')"
ip="$(ip -4 -o addr show wlan0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
cat <<MSG

FCAM overlay installed in $dest.
  status:  python3 $dest/fcam_overlay.py --status
  stop:    python3 $dest/fcam_overlay.py --stop    (frees the tracker port; refuses an instance that may use raw mode)
  start:   python3 $dest/fcam_overlay.py --start   (after stopping it; with the options installed here)
  log:     $dest/fcam_overlay.log
  remove:  python3 $dest/fcam_overlay.py --uninstall
  Baballonia face camera address: fcam://${ip:-<headset-ip>}:8555
  texture mode: ${mode:-unknown} (change: bash $here/install-overlay.sh --texture-mode file|gl; without it: auto, the default)
  texture path in use: grep -E 'GL texture path|panel texture' $dest/fcam_overlay.log | tail -3
MSG
if [ ! -d /sys/module/cdc_acm ]; then
    echo "  cdc-acm driver not loaded: build it and have it loaded at every boot (one sudo password,"
    echo "  in a terminal on the headset, ssh -t):"
    echo "    bash $here/../build-cdc-acm.sh --install"
fi
# The udev rule 0.1.x told you to install lets any program running as steamos load kernel code.
OLD_RULE=/etc/udev/rules.d/90-babble-tracker.rules
if [ -e "$OLD_RULE" ] || [ -L "$OLD_RULE" ]; then
    root_dir="$(cd "$here/.." && pwd)/root"
    cat >&2 <<MSG

WARNING: $OLD_RULE from Babble-Bridge 0.1.x is still installed.
  It has root load ~/cdc-acm/cdc-acm.ko whenever the tracker (or any ESP32 board) is plugged in,
  and any program running as steamos can replace that file. Remove it (one sudo password, ssh -t):
    sudo /usr/bin/bash $root_dir/install-cdc-acm-loader.sh   (installs the new loader, removes the rule)
  or only remove it:
    sudo rm $OLD_RULE && sudo udevadm control --reload
MSG
fi
