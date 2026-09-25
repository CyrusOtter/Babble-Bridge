#!/bin/sh
# Launcher for the FCAM overlay. SteamVR runs this through fcam.vrmanifest (binary_path_linux_arm);
# it also works from a shell. Arguments are passed on to fcam_overlay.py.
cd "$(dirname "$0")" || exit 1
exec /usr/bin/python3 ./fcam_overlay.py "$@"
