# Babble Bridge for the Steam Frame

Use a Babble face tracker that is plugged into a **Steam Frame** with Babble
([OTT-Labs/Babble](https://git.ott-labs.de/OTT-Labs/Babble), our fork of Baballonia)
running on your PC.

```
Babble tracker ──USB-C (CDC-ACM)──> Steam Frame ──Wi-Fi (FCAM/UDP)──> PC: Babble
                                    fcam_bridge.py                    fcam://<headset-ip>:8555
```

The Frame's rear USB-C port powers the tracker and enumerates it, but the stock
kernel has no serial class driver and Babble does not run on the headset. The
bridge reads the tracker's JPEG stream from `/dev/ttyACM0` and sends it to the
PC over UDP with the small FCAM protocol described in [PROTOCOL.md](PROTOCOL.md).
On the PC, Babble's `FcamStreamCapture` module receives it like any other camera.

It runs as a **SteamVR overlay application** on the headset: SteamVR starts it at
boot, and a **FCAM Bridge** tab in the dashboard shows the tracker state, frame
rate, connected clients, the address to enter in Babble and a "Restart bridge"
button. Everything is Python standard library only; nothing has to be installed
on the headset.

## What you need on the headset

- SSH access as `steamos` (Developer mode).
- `/dev/ttyACM0` when the tracker is plugged in. The stock kernel lacks the
  `cdc-acm` driver; `build-cdc-acm.sh` builds it on the headset (see
  [Building cdc-acm.ko](#building-cdc-acmko)) and the udev rule loads it on
  plug-in and lets the `steamos` user open the port.
- Wi-Fi on the same network as the PC. The Frame's default firewall zone already
  allows UDP 1024–65535 in.

## Install (SteamVR overlay)

From a release tarball, on the headset:

```sh
VERSION=0.1.0
curl -fsSL https://s3.ott-labs.de/ott-labs/babble-bridge/${VERSION}/babble-bridge-${VERSION}.tar.gz | tar xz
bash babble-bridge-${VERSION}/overlay/install-overlay.sh
```

Or from a checkout, from the PC:

```sh
scp -r Babble-Bridge steamos@<headset-ip>:~/
ssh steamos@<headset-ip> 'bash ~/Babble-Bridge/overlay/install-overlay.sh'
```

`install-overlay.sh` copies the files to `~/fcam`, registers `fcam.vrmanifest`
in SteamVR's `~/.config/openvr/config/appconfig.json`, enables auto-launch and
starts the overlay (through SteamVR when it is running, headless otherwise; it
attaches to SteamVR as soon as it comes up). No root is needed for this part.

If `/dev/ttyACM0` does not show up when the tracker is plugged in, build and
load the driver (see the next section).

## Building cdc-acm.ko

The Steam Frame kernel has no USB serial class driver, so a Babble tracker
enumerates on the rear USB-C port but gets no `/dev/ttyACM0`. `build-cdc-acm.sh`
builds `cdc-acm.ko` on the headset, as `steamos`, without root and without
touching the read-only rootfs:

```sh
bash ~/Babble-Bridge/build-cdc-acm.sh --load        # from the checkout or the release tarball
```

What it does:

1. Fetches the `linux-*-deckard-headers` package that matches the running
   kernel with `pacman -Sp` from the repositories already configured on the
   headset (their URLs are private; the script never prints them) and unpacks
   it under `~/kbuild`. It refuses to build if the repository's headers version
   differs from the running kernel.
2. Downloads `drivers/usb/class/cdc-acm.{c,h}` of the same upstream kernel
   version (`6.18.0-g…` → tag `v6.18`) from kernel.org, GitHub as fallback, into
   `~/cdc-acm`. Pass `--source DIR` to use copies fetched on another machine.
3. Builds with `make -C ~/kbuild/.../build M=~/cdc-acm modules` (gcc, make and
   binutils are on the headset). BTF is skipped because `pahole` is not
   available; the module loads fine without it.
4. Verifies the module's `vermagic` against `uname -r`. With `--load` it runs
   `sudo insmod` (asks for the password) and lists `/dev/ttyACM*`.

Result: `/home/steamos/cdc-acm/cdc-acm.ko`, the path the udev rule uses. Make
loading automatic (once, as root):

```sh
sudo install -m 0644 ~/Babble-Bridge/90-babble-tracker.rules /etc/udev/rules.d/
sudo udevadm control --reload
```

After an OS update the kernel version changes and the module stops loading;
`build-cdc-acm.sh --check` tells you, and a plain `build-cdc-acm.sh` rebuilds
it once the matching headers are in the repository.

Check it:

```sh
python3 ~/fcam/fcam_overlay.py --status      # installed / auto-launch / pid
tail -f ~/fcam/fcam_overlay.log              # bridge + overlay log
python3 ~/fcam/fcam_overlay.py --uninstall   # remove the registration and stop it
```

You should see `opened /dev/ttyACM0` and, once Babble connects,
`subscriber <pc-ip>:<port> joined (Baballonia)`. On the headset, the dashboard
gets a **FCAM Bridge** tab.

### Alternative: systemd user unit (no overlay)

`install-headset.sh` installs the plain bridge as a user service instead
(`fcam-bridge.service`, logs in `journalctl --user -u fcam-bridge`). Use one or
the other; both bind UDP 8555.

## Use in Babble

1. Home page, **Face camera** address: `fcam://<headset-ip>:8555`, then Start.
   The preferred backend shows **FCAM UDP Stream**; leave it on that or Default.
2. Crop and calibrate as with a USB tracker.

No firewall rule is needed on the PC: Babble sends a subscribe datagram first,
so the frames arrive as replies. Only listen-only mode (`fcam://:8555` in Babble
plus `--target <pc-ip>` on the bridge) needs an inbound UDP rule.

## Bridge options

```
python3 fcam_bridge.py [--serial auto|/dev/ttyACMn|FILE] [--usb-id 303a:1001] [--usb-serial S]
                       [--baud 3000000] [--listen 0.0.0.0:8555] [--target PC[:PORT]]...
                       [--chunk 1400] [--sub-ttl 3] [--status-interval 1]
                       [--stats SECONDS] [--log-tracker-text] [-v]
```

**Device discovery.** `--serial auto` (the default) looks the tracker up through
sysfs by USB vendor:product (`303a:1001`, the ESP32-S3 in the Babble tracker) on
every open, so it does not matter whether the port shows up as `/dev/ttyACM0`
or, after the tracker re-enumerated, as `/dev/ttyACM1`. When several matching
devices are plugged in, the unit used last is preferred and `--usb-serial`
pins one. A fixed path still works: `/dev/babble-tracker` (from the udev rule)
or `/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_<serial>-if00`.

`fcam_overlay.py` accepts the same options plus `--install`, `--uninstall`,
`--status`, `--no-launch`, `--openvr-lib`, `--log-file`. The manifest launches it
without arguments, so edit `fcam.vrmanifest` (`arguments`) if the defaults
need changing.

`--serial` may also be a FIFO, or a recorded file (replayed at `--replay-fps`,
default 30), which is how the bridge is tested without a headset.

## Layout

| Path | Purpose |
|---|---|
| `fcam_bridge.py` | The bridge: ETVR serial framing → FCAM/UDP. Also usable standalone. |
| `overlay/fcam_overlay.py` | SteamVR overlay app: runs the bridge in-process, draws the dashboard tab, registers itself. |
| `overlay/openvr_min.py` | `ctypes` binding to the runtime's own arm64 `libopenvr_api.so` (IVRSystem_026, IVROverlay_028, IVRApplications_008). |
| `overlay/fcam_font.py`, `overlay/icon.png` | Generated by `overlay/tools/` on a PC with Pillow. |
| `overlay/fcam.vrmanifest`, `overlay/fcam_overlay.sh` | SteamVR application manifest (`binary_path_linux_arm`) and its launcher. |
| `overlay/install-overlay.sh` | Installer (no root). |
| `fcam-bridge.service`, `install-headset.sh` | systemd alternative. |
| `build-cdc-acm.sh` | Builds `cdc-acm.ko` for the running kernel on the headset (no root). |
| `90-babble-tracker.rules` | udev rule: load `cdc-acm.ko`, `uaccess` on the port (root, once). |
| `PROTOCOL.md` | Wire format. |
| `tests/` | `python3 -m unittest discover -s tests` |

## Development and testing

Record a few seconds of the raw serial stream on any Linux box with the tracker:

```sh
python3 - <<'EOF'
import os, termios, time
fd = os.open("/dev/ttyACM0", os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
a = termios.tcgetattr(fd); a[0] = a[1] = a[3] = 0; a[4] = a[5] = termios.B3000000
termios.tcsetattr(fd, termios.TCSANOW, a)
out = open("tracker.etvr", "wb"); t = time.time()
while time.time() - t < 3:
    try: out.write(os.read(fd, 65536))
    except BlockingIOError: time.sleep(0.005)
EOF
```

Then replay it on the PC and point Babble (or its `tools/FcamProbe`) at it:

```sh
python fcam_bridge.py --serial tracker.etvr --listen 127.0.0.1:8555 --stats 5
```

The overlay itself only runs on the headset (it needs the SteamVR runtime), but
its panel rendering and registration logic are covered by the tests.

## Releases

CI (`.forgejo/workflows/ci.yml`) runs the tests on every push. Tagging `vX.Y.Z`
(after setting `__version__` in `fcam_bridge.py` to match) builds
`babble-bridge-X.Y.Z.tar.gz`, attaches it to a Forgejo release and uploads it
to `https://s3.ott-labs.de/ott-labs/babble-bridge/X.Y.Z/` for the headset to
fetch without credentials.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Log says `cannot open /dev/ttyACM0: No such file` | `cdc-acm` not loaded, or the tracker is not enumerated. `lsusb` should list `303a:1001`; then `build-cdc-acm.sh --load`, or install the udev rule. |
| `Permission denied` on the port | Install the udev rule (adds the `uaccess` tag) or `sudo chmod 660 /dev/ttyACM0` for a quick test. |
| Babble stays on "connecting", no `subscriber ... joined` in the log | PC and headset are not on the same network, or the address is wrong. |
| Subscriber joins, no frames, status says `no-source` | The tracker is not streaming over USB. Stock Babble/OpenIris firmware streams serial only when it is not in Wi-Fi streaming mode. |
| `... closed, waiting for it to come back` repeatedly | The tracker is re-enumerating on USB (power, hub or cable). The bridge finds it again by USB id, whichever `ttyACMn` it gets. |
| `overlay upload failed ... RequestFailed` once | SteamVR's compositor is in standby (headset off). Uploads resume when it wakes. |
| Bridge stops after headset standby | Expected when SteamVR shuts down; it is relaunched when SteamVR starts. |
