#!/usr/bin/env bash
# Installs the FCAM bridge as a user service on a Steam Frame.
# Run ON the headset as the steamos user, from the directory that holds this script:
#
#   scp -r Babble-Bridge steamos@<headset>:~/ && ssh steamos@<headset> 'bash ~/Babble-Bridge/install-headset.sh'
#
# No root is needed for this part. The one-time driver step (cdc-acm, one sudo password) is
# printed at the end.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="$HOME/fcam"
unit_dir="$HOME/.config/systemd/user"

mkdir -p "$dest" "$unit_dir"
install -m 0755 "$here/fcam_bridge.py" "$dest/fcam_bridge.py"
install -m 0644 "$here/fcam-bridge.service" "$unit_dir/fcam-bridge.service"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
systemctl --user daemon-reload
systemctl --user enable --now fcam-bridge.service
sleep 1
systemctl --user --no-pager --lines=5 status fcam-bridge.service || true

ip="$(ip -4 -o addr show wlan0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
cat <<MSG

FCAM bridge installed and enabled as a user service.
  logs:    journalctl --user -u fcam-bridge -f
  address: fcam://${ip:-<headset-ip>}:8555   (enter this as the face camera in Baballonia)

One-time driver step, if /dev/ttyACM0 does not appear when the tracker is plugged in (the
stock kernel has no cdc-acm driver). In a terminal on the headset (ssh -t), it asks once for
the sudo password and then loads the driver at every boot:
  bash $here/build-cdc-acm.sh --install
MSG
# The udev rule 0.1.x told you to install lets any program running as steamos load kernel code.
OLD_RULE=/etc/udev/rules.d/90-babble-tracker.rules
if [ -e "$OLD_RULE" ] || [ -L "$OLD_RULE" ]; then
    cat >&2 <<MSG

WARNING: $OLD_RULE from Babble-Bridge 0.1.x is still installed.
  It has root load ~/cdc-acm/cdc-acm.ko whenever the tracker (or any ESP32 board) is plugged in,
  and any program running as steamos can replace that file. Remove it (one sudo password, ssh -t):
    sudo /usr/bin/bash $here/root/install-cdc-acm-loader.sh   (installs the new loader, removes the rule)
  or only remove it:
    sudo rm $OLD_RULE && sudo udevadm control --reload
MSG
fi
