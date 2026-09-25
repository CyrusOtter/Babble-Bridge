#!/usr/bin/env python3
"""
fcam_overlay.py - the FCAM bridge as a SteamVR overlay application for the Steam Frame.

Runs fcam_bridge in-process (tracker on /dev/ttyACM0 -> Baballonia over UDP) and shows its state
as a dashboard overlay: tracker presence, frame rate, subscribers, the address to enter in
Baballonia, and a button to restart the bridge. SteamVR starts it at boot once it is registered
with --install (fcam.vrmanifest + auto-launch) and asks it to quit at shutdown.

Standard library only. Works headless too: while SteamVR is not running the bridge keeps
streaming and the overlay connects as soon as SteamVR comes up.

    fcam_overlay.py [bridge options]      run (what SteamVR launches through fcam_overlay.sh)
    fcam_overlay.py --install             register with SteamVR, enable auto-launch, start it
    fcam_overlay.py --uninstall           remove the registration
    fcam_overlay.py --status              show registration state
"""

import argparse
import json
import logging
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

try:
    import fcntl
except ImportError:  # not Linux: no single-instance lock (development only)
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (HERE, os.path.dirname(HERE)):  # installed: everything in one dir; repo: bridge one level up
    if _path not in sys.path:
        sys.path.insert(0, _path)

import fcam_bridge  # noqa: E402
import fcam_font  # noqa: E402
import openvr_min as ovr  # noqa: E402

APP_KEY = "ottlabs.fcam"
OVERLAY_KEY = "ottlabs.fcam.dashboard"
OVERLAY_NAME = "FCAM Bridge"
MANIFEST = os.path.join(HERE, "fcam.vrmanifest")
ICON = os.path.join(HERE, "icon.png")
DEFAULT_LOG = os.path.join(HERE, "fcam_overlay.log")
LOCK = os.path.join(HERE, "fcam_overlay.lock")

PANEL_W, PANEL_H = 640, 400
PANEL_FILE_DIR = "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir()
PNG_SIGNATURE = bytes((137, 80, 78, 71, 13, 10, 26, 10))
BUTTON_H = 64
VR_RETRY_SECONDS = 5.0
REDRAW_SECONDS = 0.5          # while the tab is visible
REDRAW_HIDDEN_SECONDS = 5.0   # while hidden, or after a failed upload (standby)
EVENT_POLL_SECONDS = 0.05

# RGBA colours
BG = (24, 32, 44, 255)
PANEL = (34, 44, 58, 255)
TEXT = (230, 236, 240, 255)
DIM = (150, 160, 170, 255)
ACCENT = (78, 160, 168, 255)
OK = (96, 200, 120, 255)
WARN = (240, 180, 70, 255)
BAD = (230, 90, 90, 255)
BUTTON = (60, 110, 120, 255)
BUTTON_HOT = (90, 150, 160, 255)

log = logging.getLogger("fcam.overlay")


# ----------------------------------------------------------------------------- software renderer

class Panel:
    """An RGBA8 raster the size of the overlay, drawn with the generated bitmap font."""

    def __init__(self, width, height):
        self.width, self.height = width, height
        self.buf = bytearray(width * height * 4)

    def fill(self, rgba, x0=0, y0=0, x1=None, y1=None):
        x1 = self.width if x1 is None else min(x1, self.width)
        y1 = self.height if y1 is None else min(y1, self.height)
        x0, y0 = max(0, x0), max(0, y0)
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes(rgba) * (x1 - x0)
        stride = self.width * 4
        for y in range(y0, y1):
            start = y * stride + x0 * 4
            self.buf[start:start + len(row)] = row

    def outline(self, rgba, x0, y0, x1, y1, thickness=2):
        self.fill(rgba, x0, y0, x1, y0 + thickness)
        self.fill(rgba, x0, y1 - thickness, x1, y1)
        self.fill(rgba, x0, y0, x0 + thickness, y1)
        self.fill(rgba, x1 - thickness, y0, x1, y1)

    def text(self, x, y, string, rgba=TEXT, scale=1):
        cell_w, cell_h = fcam_font.CELL_W, fcam_font.CELL_H
        pixel = bytes(rgba)
        stride = self.width * 4
        for index, ch in enumerate(string):
            gx = x + index * cell_w * scale
            if gx >= self.width:
                break
            for ry, row in enumerate(fcam_font.glyph_rows(ch)):
                if not row:
                    continue
                for rx in range(cell_w):
                    if row >> rx & 1:
                        px, py = gx + rx * scale, y + ry * scale
                        for dy in range(scale):
                            yy = py + dy
                            if 0 <= yy < self.height:
                                start = yy * stride + px * 4
                                self.buf[start:start + 4 * scale] = pixel * scale

    def text_width(self, string, scale=1):
        return len(string) * fcam_font.CELL_W * scale

    def to_png(self):
        """Encodes the raster as an RGBA PNG (standard library only)."""
        stride = self.width * 4
        raw = bytearray()
        for y in range(self.height):
            raw.append(0)  # filter type: none
            raw += self.buf[y * stride:(y + 1) * stride]

        def chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        return (PNG_SIGNATURE
                + chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(raw), 1))
                + chunk(b"IEND", b""))


def local_ip():
    """The address a PC on the LAN would use to reach this headset."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # no packet is sent for UDP connect
            return probe.getsockname()[0]
    except OSError:
        return socket.gethostname()


def draw_panel(panel, snapshot, hot_button, vr_version):
    panel.fill(BG)
    panel.fill(PANEL, 16, 16, PANEL_W - 16, PANEL_H - 16 - BUTTON_H - 12)

    panel.text(32, 28, "FCAM Bridge", ACCENT, scale=2)
    state = snapshot.get("state", "?")
    state_color = {"streaming": OK, "opening": WARN, "no-source": BAD, "stopping": DIM}.get(state, DIM)
    state_text = {"streaming": "tracker streaming", "opening": "opening tracker",
                  "no-source": "no tracker (" + snapshot.get("source", "?") + ")", "stopping": "stopping"}.get(state, state)
    panel.text(32, 90, state_text, state_color, scale=1)

    rows = [
        ("address", "fcam://%s:%d" % (local_ip(), snapshot.get("listen_port", fcam_bridge.DEFAULT_PORT))),
        ("tracker", "%.1f fps, %d frames, %d bad" % (snapshot.get("fps", 0.0), snapshot.get("frames", 0),
                                                     snapshot.get("bad", 0))),
        ("dropped", "%d (queue), %d unsent, %d send errors" % (snapshot.get("dropped", 0), snapshot.get("unsent", 0),
                                                              snapshot.get("send_errors", 0))),
        ("clients", ", ".join(snapshot.get("subscribers", [])) or "none (start the face camera in Baballonia)"),
        ("uptime", "%ds, port opened %d time(s)" % (snapshot.get("uptime", 0), snapshot.get("opens", 0))),
    ]
    y = 130
    for label, value in rows:
        panel.text(32, y, label.rjust(8), DIM)
        panel.text(32 + 9 * fcam_font.CELL_W, y, value[:44], TEXT)
        y += fcam_font.CELL_H + 8

    panel.text(32, PANEL_H - BUTTON_H - 16 - 30, "SteamVR %s" % (vr_version or "?"), DIM)

    bx0, by0, bx1, by1 = 16, PANEL_H - 16 - BUTTON_H, PANEL_W - 16, PANEL_H - 16
    panel.fill(BUTTON_HOT if hot_button else BUTTON, bx0, by0, bx1, by1)
    panel.outline(ACCENT, bx0, by0, bx1, by1)
    label = "Restart bridge"
    panel.text((PANEL_W - panel.text_width(label, 2)) // 2, by0 + (BUTTON_H - fcam_font.CELL_H * 2) // 2, label,
               TEXT, scale=2)


def in_button(x, y):
    """Hit test for the restart button. Mouse y may be top-down or bottom-up depending on runtime
    conventions, so the bottom band is accepted in both orientations; nothing else sits opposite it."""
    if not 0 <= x <= PANEL_W:
        return False
    band = BUTTON_H + 16
    return y <= band or y >= PANEL_H - band


# ----------------------------------------------------------------------------- overlay app

class OverlayApp:
    def __init__(self, args):
        self.args = args
        self.runtime = fcam_bridge.BridgeRuntime(args)
        self.session = None
        self.main_handle = None
        self.thumb_handle = None
        self.panel = Panel(PANEL_W, PANEL_H)
        self.stop = threading.Event()
        self.hot_button = False
        self.last_vr_attempt = 0.0
        self.vr_error_logged = None
        self.upload_failures = 0
        self.upload_failed_since = None
        self.panel_file = os.path.join(PANEL_FILE_DIR, "fcam-panel-%d.png" % os.getpid())
        self.was_visible = False
        self.texture_confirmed = False

    # -- SteamVR connection

    def connect_vr(self):
        now = time.monotonic()
        if now - self.last_vr_attempt < VR_RETRY_SECONDS:
            return False
        self.last_vr_attempt = now
        app_type = ovr.VRApplication_Background if self.args.app_type == "background" else ovr.VRApplication_Overlay
        try:
            session = ovr.Session(self.args.openvr_lib).init(app_type)
        except ovr.OpenVRError as e:
            message = str(e)
            if message != self.vr_error_logged:
                log.info("SteamVR not available yet: %s", message)
                self.vr_error_logged = message
            return False
        self.vr_error_logged = None
        try:
            overlay = session.overlay
            existing = overlay.find_overlay(OVERLAY_KEY)
            if existing:
                overlay.destroy_overlay(existing)
            main, thumb = overlay.create_dashboard_overlay(OVERLAY_KEY, OVERLAY_NAME)
            overlay.set_width_meters(main, 1.5)
            overlay.set_input_method(main, ovr.VROverlayInputMethod_Mouse)
            overlay.set_mouse_scale(main, PANEL_W, PANEL_H)
            if os.path.exists(ICON) and not os.environ.get("FCAM_DEBUG_NO_THUMB"):
                try:
                    overlay.set_from_file(thumb, ICON)
                except ovr.OpenVRError as e:
                    log.warning("thumbnail not set: %s", e)
            try:
                session.applications.identify(os.getpid(), APP_KEY)
            except ovr.OpenVRError as e:
                log.debug("IdentifyApplication: %s", e)
        except ovr.OpenVRError as e:
            log.error("overlay setup failed: %s", e)
            session.shutdown()
            return False
        self.session = session
        self.main_handle, self.thumb_handle = main, thumb
        self.texture_confirmed = False
        self.texture_warned = False
        self.last_upload = 0.0
        self.was_visible = False
        log.info("SteamVR connected (%s, %s, %s, runtime %s, %s)", session.system.version, session.overlay.version,
                 session.applications.version, session.system.runtime_version(), session.library_path)
        self.redraw(force=True)
        return True

    def disconnect_vr(self):
        if self.session is None:
            return
        # FCAM_DEBUG_EXIT_MODE: "clear" clears the texture before destroying the overlay (default),
        # "nodestroy" only shuts the session down, "destroy" destroys without clearing.
        mode = os.environ.get("FCAM_DEBUG_EXIT_MODE", "clear")
        try:
            if self.main_handle and mode in ("clear", "clear-nodestroy"):
                self.session.overlay.clear_texture(self.main_handle)
            if self.main_handle and mode in ("clear", "destroy"):
                self.session.overlay.destroy_overlay(self.main_handle)
        except ovr.OpenVRError as e:
            log.debug("overlay teardown: %s", e)
        if mode.endswith("sleep"):
            time.sleep(1.0)
        self.session.shutdown()
        self.session = None
        self.main_handle = self.thumb_handle = None

    # -- drawing and input

    def redraw(self, force=False):
        """Draws the panel and uploads it. Returns True when the upload succeeded."""
        if self.session is None:
            return False
        snapshot = self.runtime.snapshot()
        version = self.session.system.runtime_version()
        draw_panel(self.panel, snapshot, self.hot_button, version)
        if os.environ.get("FCAM_DEBUG_NO_TEXTURE"):
            return True  # diagnostics: keep the overlay without ever uploading a texture
        try:
            if self.args.texture_mode == "raw":
                self.session.overlay.set_raw(self.main_handle, self.panel.buf, PANEL_W, PANEL_H)
            else:
                # SetOverlayFromFile makes SteamVR load the image itself. SetOverlayRaw leaves the
                # compositor holding a client-owned buffer, and vrcompositor on the Steam Frame
                # (SteamVR 2.17.10) crashes when that client exits.
                tmp = self.panel_file + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(self.panel.to_png())
                os.replace(tmp, self.panel_file)
                self.session.overlay.set_from_file(self.main_handle, self.panel_file)
        except (ovr.OpenVRError, OSError) as e:
            # Happens while the compositor is suspended (headset in standby); log once per episode.
            self.upload_failures += 1
            if self.upload_failed_since is None:
                self.upload_failed_since = time.monotonic()
                log.warning("overlay upload failed (%s); retrying every %.0f s", e, REDRAW_HIDDEN_SECONDS)
            return False
        if self.upload_failed_since is not None:
            log.info("overlay uploads work again after %.0f s (%d failed)",
                     time.monotonic() - self.upload_failed_since, self.upload_failures)
            self.upload_failed_since = None
        self.last_upload = time.monotonic()
        return True

    def confirm_texture(self):
        """SteamVR loads SetOverlayFromFile images asynchronously; ask for the size a moment later."""
        if self.texture_confirmed or self.session is None or not self.last_upload:
            return
        age = time.monotonic() - self.last_upload
        if age < 1.0:
            return
        try:
            width, height = self.session.overlay.texture_size(self.main_handle)
            log.info("panel texture %dx%d set (%s mode)", width, height, self.args.texture_mode)
            self.texture_confirmed = True
        except ovr.OpenVRError as e:
            if age > 10.0 and not self.texture_warned:
                log.warning("panel texture still not confirmed after %.0f s: %s", age, e)
                self.texture_warned = True

    def restart_bridge(self):
        log.info("restarting bridge on request")
        self.runtime.stop()
        time.sleep(0.5)  # let the old socket close before binding the same port again
        try:
            self.runtime = fcam_bridge.BridgeRuntime(self.args)
        except OSError as e:
            log.error("bridge restart failed: %s", e)
            return
        self.runtime.start()

    def handle_events(self):
        """Returns False when SteamVR asked us to quit."""
        session = self.session
        while True:
            event = session.system.poll_next_event()
            if event is None:
                break
            # Only VREvent_Quit is addressed to us; ProcessQuit (701) is broadcast whenever any
            # other VR process exits and must be ignored.
            if event.eventType == ovr.VREvent_Quit:
                log.info("SteamVR requested quit")
                session.system.acknowledge_quit_exiting()
                return False
        while True:
            event = session.overlay.poll_next_overlay_event(self.main_handle)
            if event is None:
                break
            if event.eventType == ovr.VREvent_MouseMove:
                x, y, _ = event.mouse()
                hot = in_button(x, y)
                if hot != self.hot_button:
                    self.hot_button = hot
                    self.redraw()
            elif event.eventType == ovr.VREvent_MouseButtonDown:
                x, y, button = event.mouse()
                if in_button(x, y):
                    self.restart_bridge()
                    self.redraw()
            elif event.eventType == ovr.VREvent_OverlayHidden:
                self.hot_button = False
        return True

    # -- main loop

    def run(self):
        log.info("FCAM overlay starting (pid %d)", os.getpid())
        self.runtime.start()
        next_redraw = 0.0
        try:
            while not self.stop.is_set():
                if self.session is None:
                    self.connect_vr()
                    self.stop.wait(0.5 if self.session is None else 0)
                    continue
                try:
                    if not self.handle_events():
                        break
                    now = time.monotonic()
                    self.confirm_texture()
                    if now >= next_redraw:
                        visible = self.session.overlay.is_visible(self.main_handle)
                        if visible:
                            ok = self.redraw()  # live updates while the tab is shown
                            next_redraw = now + (REDRAW_SECONDS if ok else REDRAW_HIDDEN_SECONDS)
                        elif self.was_visible:
                            self.redraw()  # one last refresh as the tab goes away; SteamVR keeps the texture
                            next_redraw = now + REDRAW_SECONDS
                        else:
                            next_redraw = now + REDRAW_SECONDS  # hidden: just keep polling visibility
                        self.was_visible = visible
                except ovr.OpenVRError as e:
                    log.warning("SteamVR call failed, reconnecting: %s", e)
                    self.disconnect_vr()
                    continue
                self.stop.wait(EVENT_POLL_SECONDS)
        finally:
            self.disconnect_vr()
            self.runtime.stop()
            for path in (self.panel_file, self.panel_file + ".tmp"):
                try:
                    os.remove(path)
                except OSError:
                    pass
            log.info("FCAM overlay stopped")


# ----------------------------------------------------------------------------- single instance

def acquire_lock():
    """Holds an exclusive lock for this install directory. Returns the lock file, or None when another
    instance already runs (for example a headless one started before SteamVR auto-launched us)."""
    if fcntl is None:
        return open(os.devnull, "w")
    handle = open(LOCK, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def running_pid():
    """Pid of the instance holding the lock, or 0."""
    if fcntl is None or not os.path.exists(LOCK):
        return 0
    try:
        with open(LOCK) as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return int(handle.read().strip() or 0)
            fcntl.flock(handle, fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass
    return 0


def start_detached():
    """Starts the overlay headless in its own session, surviving the caller's logout."""
    with open(os.devnull, "rb") as stdin, open(os.devnull, "ab") as stdout:
        process = subprocess.Popen([sys.executable, os.path.abspath(__file__)], cwd=HERE, stdin=stdin,
                                   stdout=stdout, stderr=stdout, start_new_session=True, close_fds=True)
    return process.pid


# ----------------------------------------------------------------------------- registration

def config_dir():
    path = os.path.expanduser("~/.config/openvr/openvrpaths.vrpath")
    try:
        with open(path, encoding="utf-8") as fh:
            dirs = json.load(fh).get("config") or []
        if dirs:
            return dirs[0]
    except (OSError, ValueError):
        pass
    return os.path.expanduser("~/.config/openvr/config")


def register_files(enable):
    """Registers (or removes) the manifest and auto-launch flag directly in SteamVR's config files,
    which SteamVR reads at startup. Used so the install works with SteamVR stopped too."""
    cfg = config_dir()
    os.makedirs(os.path.join(cfg, "vrappconfig"), exist_ok=True)
    appconfig = os.path.join(cfg, "appconfig.json")
    try:
        with open(appconfig, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    paths = [p for p in data.get("manifest_paths", []) if os.path.abspath(p) != os.path.abspath(MANIFEST)]
    if enable:
        paths.append(os.path.abspath(MANIFEST))
    data["manifest_paths"] = paths
    with open(appconfig, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=3)
    appcfg = os.path.join(cfg, "vrappconfig", APP_KEY + ".vrappconfig")
    with open(appcfg, "w", encoding="utf-8") as fh:
        json.dump({"autolaunch": bool(enable), "last_launch_time": "0"}, fh, indent=3)
    return appconfig, appcfg


def register_live(enable, launch, library):
    """Same through the running SteamVR, so it takes effect immediately. Returns a status text."""
    try:
        session = ovr.Session(library).init(ovr.VRApplication_Background)
    except ovr.OpenVRError as e:
        note = "SteamVR not running (%s); registration takes effect at the next SteamVR start" % e
        if enable and launch:
            pid = running_pid()
            if pid:
                note += "; overlay already running headless (pid %d)" % pid
            else:
                note += "; started headless (pid %d), it connects when SteamVR comes up" % start_detached()
        elif not enable:
            pid = running_pid()
            if pid:
                os.kill(pid, signal.SIGTERM)
                note += "; stopped pid %d" % pid
        return note
    try:
        apps = session.applications
        if enable:
            apps.add_manifest(MANIFEST)
            apps.set_auto_launch(APP_KEY, True)
            state = "registered with SteamVR, auto-launch %s" % apps.get_auto_launch(APP_KEY)
            if launch:
                pid = apps.process_id(APP_KEY) or running_pid()
                if pid:
                    state += ", already running (pid %d)" % pid
                else:
                    apps.launch(APP_KEY)
                    time.sleep(2.0)
                    state += ", launched (pid %d)" % apps.process_id(APP_KEY)
            return state
        pid = apps.process_id(APP_KEY) or running_pid()
        if pid:
            os.kill(pid, signal.SIGTERM)
        apps.set_auto_launch(APP_KEY, False)
        apps.remove_manifest(MANIFEST)
        return "removed from SteamVR" + (", stopped pid %d" % pid if pid else "")
    except ovr.OpenVRError as e:
        return "SteamVR registration failed: %s" % e
    finally:
        session.shutdown()


def status(library):
    cfg = config_dir()
    lines = ["manifest: %s" % MANIFEST, "config dir: %s" % cfg, "running instance (lock): pid %d" % running_pid()]
    try:
        with open(os.path.join(cfg, "appconfig.json"), encoding="utf-8") as fh:
            lines.append("appconfig.json lists it: %s" %
                         (os.path.abspath(MANIFEST) in map(os.path.abspath, json.load(fh).get("manifest_paths", []))))
    except (OSError, ValueError):
        lines.append("appconfig.json: unreadable")
    try:
        session = ovr.Session(library).init(ovr.VRApplication_Background)
        apps = session.applications
        lines.append("SteamVR: installed=%s autolaunch=%s pid=%d" %
                     (apps.is_installed(APP_KEY), apps.get_auto_launch(APP_KEY), apps.process_id(APP_KEY)))
        session.shutdown()
    except ovr.OpenVRError as e:
        lines.append("SteamVR: not reachable (%s)" % e)
    return "\n".join(lines)


# ----------------------------------------------------------------------------- entry point

def truncate_log(path, limit=1024 * 1024):
    try:
        if os.path.getsize(path) > limit:
            os.replace(path, path + ".old")
    except OSError:
        pass


def main(argv=None):
    parser = fcam_bridge.build_parser()
    parser.description = "FCAM bridge as a SteamVR overlay."
    parser.add_argument("--install", action="store_true", help="register with SteamVR, enable auto-launch and start")
    parser.add_argument("--uninstall", action="store_true", help="remove the SteamVR registration and stop")
    parser.add_argument("--status", action="store_true", help="print registration state and exit")
    parser.add_argument("--no-launch", action="store_true", help="with --install: do not start the overlay now")
    parser.add_argument("--openvr-lib", default=None, help="path to libopenvr_api.so (auto-detected)")
    parser.add_argument("--texture-mode", choices=("file", "raw"), default="file",
                        help="how the panel reaches SteamVR: 'file' (PNG via SetOverlayFromFile, default) or 'raw' "
                             "(SetOverlayRaw; crashes the Steam Frame compositor when the overlay exits)")
    parser.add_argument("--app-type", choices=("overlay", "background"), default="overlay",
                        help="how to identify to SteamVR (VRApplication_Overlay or _Background)")
    parser.add_argument("--log-file", default=DEFAULT_LOG, help="log file (default: next to this script)")
    args = parser.parse_args(argv)

    handlers = [logging.StreamHandler()]
    if args.log_file:
        truncate_log(args.log_file)
        handlers.append(logging.FileHandler(args.log_file))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%H:%M:%S",
                        handlers=handlers)

    if args.status:
        print(status(args.openvr_lib))
        return 0
    if args.install or args.uninstall:
        enable = bool(args.install)
        appconfig, appcfg = register_files(enable)
        print("%s %s and %s" % ("updated" if enable else "cleaned", appconfig, appcfg))
        print(register_live(enable, launch=not args.no_launch, library=args.openvr_lib))
        return 0

    lock = acquire_lock()
    if lock is None:
        log.info("another FCAM overlay instance is running (pid %d); exiting", running_pid())
        return 0

    try:
        app = OverlayApp(args)
    except OSError as e:
        log.error("cannot start the bridge (%s); is UDP port %s already in use?", e, args.listen)
        lock.close()
        return 1

    def request_stop(signum, _frame):
        log.info("signal %d, stopping", signum)
        app.stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    app.run()
    lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
