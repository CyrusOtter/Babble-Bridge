#!/usr/bin/env bash
# Installs the FCAM bridge as a user service on a Steam Frame.
# Run ON the headset as the steamos user, from the directory that holds this script:
#
#   scp -r Babble-Bridge steamos@<headset>:~/ && ssh steamos@<headset> 'bash ~/Babble-Bridge/install-headset.sh'
#
# No root is needed for this part. The one-time root steps (cdc-acm module + udev
# rule) are printed at the end.
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

One-time root steps, if /dev/ttyACM0 does not appear when the tracker is plugged in
(the stock kernel has no cdc-acm driver):
  sudo insmod /home/steamos/cdc-acm/cdc-acm.ko
  sudo install -m 0644 $here/90-babble-tracker.rules /etc/udev/rules.d/
  sudo udevadm control --reload
MSG
