"""Renders the dashboard panel shown in the README (docs/overlay-panel.png) with the overlay's own
drawing code, so the screenshot matches what SteamVR displays. Standard library only; run from the
repository root after changing the panel:

    python overlay/tools/render_screenshot.py [output.png]
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [os.path.join(ROOT, "overlay"), ROOT]

import fcam_overlay  # noqa: E402

# A tracker streaming to one Babble client, in the default file texture mode. Example addresses only.
SNAPSHOT = {"state": "streaming", "source": "/dev/ttyACM0", "fps": 61.8, "frames": 184320, "bad": 1,
            "dropped": 0, "unsent": 0, "send_errors": 0, "subscribers": ["192.168.1.20:51734"],
            "listen_port": 8555, "uptime": 3120, "opens": 1}
STARTED = time.mktime((2026, 9, 26, 11, 2, 0, 0, 0, -1))


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "docs", "overlay-panel.png")
    panel = fcam_overlay.Panel(fcam_overlay.PANEL_W, fcam_overlay.PANEL_H)
    coarse = fcam_overlay.CoarseNumbers(wall=lambda: STARTED)
    fcam_overlay.draw_panel(panel, SNAPSHOT, False, "2.17.10", sink_label="file", ip="192.168.1.50",
                            coarse=coarse)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(panel.to_png())
    print("wrote %s (%dx%d)" % (out, fcam_overlay.PANEL_W, fcam_overlay.PANEL_H))


if __name__ == "__main__":
    main()
