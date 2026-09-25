#!/usr/bin/env bash
# Installs the FCAM bridge as a SteamVR overlay application on a Steam Frame.
# Run ON the headset as the steamos user, from a copy of this repository or a release tarball:
#
#   scp -r Babble-Bridge steamos@<headset>:~/ && ssh steamos@<headset> 'bash ~/Babble-Bridge/overlay/install-overlay.sh'
#
# Copies the files to ~/fcam, registers fcam.vrmanifest with SteamVR, enables auto-launch and
# starts the overlay. Replaces the fcam-bridge systemd user unit if that was enabled (both would
# bind UDP 8555). No root needed; the cdc-acm/udev step from the README is still separate.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="$HOME/fcam"

mkdir -p "$dest"
install -m 0755 "$here/../fcam_bridge.py" "$here/fcam_overlay.py" "$here/fcam_overlay.sh" "$dest/"
install -m 0644 "$here/openvr_min.py" "$here/fcam_font.py" "$here/icon.png" "$here/fcam.vrmanifest" "$dest/"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if systemctl --user is-enabled fcam-bridge.service >/dev/null 2>&1; then
    echo "disabling the fcam-bridge systemd unit (the overlay replaces it)"
    systemctl --user disable --now fcam-bridge.service
fi
systemctl --user stop fcam-bridge.service 2>/dev/null || true

python3 "$dest/fcam_overlay.py" --install "$@"

ip="$(ip -4 -o addr show wlan0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
cat <<MSG

FCAM overlay installed in $dest.
  status:  python3 $dest/fcam_overlay.py --status
  log:     $dest/fcam_overlay.log
  remove:  python3 $dest/fcam_overlay.py --uninstall
  Babble face camera address: fcam://${ip:-<headset-ip>}:8555
MSG
