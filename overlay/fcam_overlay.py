#!/usr/bin/env python3
"""
fcam_overlay.py - the FCAM bridge as a SteamVR overlay application for the Steam Frame.

Runs fcam_bridge in-process (tracker on /dev/ttyACM0 -> Baballonia over UDP) and shows its state
as a dashboard overlay: tracker presence, frame rate, subscribers, the address to enter in
Baballonia, and a button to restart the bridge. SteamVR starts it at boot once it is registered
with --install (fcam.vrmanifest + auto-launch) and asks it to quit at shutdown.

Standard library only. Works headless too: while SteamVR is not running the bridge keeps
streaming and the overlay connects as soon as SteamVR comes up.

How the panel reaches SteamVR (--texture-mode):
    file   PNG in $XDG_RUNTIME_DIR, SetOverlayFromFile; only when the content changed, at most every
           2 s (SteamVR loads it asynchronously). The default until the GL path passed the on-headset
           exit test (tools/gl_exit_test.sh).
    gl     one persistent GLES texture (gl_texture.py, EGL surfaceless), SetOverlayTexture; if GL
           cannot be used the tab stays blank (strict, for testing).
    auto   gl, falling back to file when GL fails.
    none   no texture at all (diagnostics).
    raw    SetOverlayRaw; only with --unsafe-raw: it crashes vrcompositor on the Steam Frame when the
           overlay exits. Never chosen automatically.

    fcam_overlay.py [bridge options]      run (what SteamVR launches through fcam_overlay.sh)
    fcam_overlay.py --install [options]   register with SteamVR, record the options in the manifest,
                                          enable auto-launch and (re)start it
    fcam_overlay.py --start               start it again with the options --install recorded
                                          (through SteamVR when it runs, headless otherwise)
    fcam_overlay.py --stop                stop it and wait until it exited (frees the tracker port),
                                          unless it may use raw mode: that one is left running
    fcam_overlay.py --uninstall           remove the registration and stop it (while SteamVR runs,
                                          an instance that may use raw mode is left running)
    fcam_overlay.py --status              show registration state
"""

import argparse
import json
import logging
import os
import shlex
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

try:
    import gl_texture  # noqa: E402  (loads no library at import time)
except ImportError:  # an install without gl_texture.py: GL is unavailable, file mode still works
    gl_texture = None

APP_KEY = "ottlabs.fcam"
OVERLAY_KEY = "ottlabs.fcam.dashboard"
OVERLAY_NAME = "FCAM Bridge"
MANIFEST = os.path.join(HERE, "fcam.vrmanifest")
ICON = os.path.join(HERE, "icon.png")
DEFAULT_LOG = os.path.join(HERE, "fcam_overlay.log")
LOCK_NAME = "ottlabs.fcam.lock"                          # in $XDG_RUNTIME_DIR: one instance per user
LEGACY_LOCK = os.path.join(HERE, "fcam_overlay.lock")   # 0.1.x: per install directory, plain pid
LEGACY_PANEL_FILE = "/dev/shm/fcam-panel-%d.png"         # written only by 0.1.x builds in file mode

TEXTURE_MODES = ("auto", "gl", "file", "none", "raw")
DEFAULT_TEXTURE_MODE = "file"   # phase 1: GL stays opt-in until tools/gl_exit_test.sh passed on the headset
RESTARTABLE_MODES = ("auto", "gl", "file", "none")
RAW_REFUSED = ("--texture-mode raw needs --unsafe-raw: SetOverlayRaw leaves vrcompositor holding a buffer "
               "that belongs to this process, and on the Steam Frame the compositor dies with SIGBUS as soon "
               "as the overlay exits (gamescope then crash-loops until the overlay is gone). Use auto, gl or file.")

PANEL_W, PANEL_H = 640, 400
PNG_SIGNATURE = bytes((137, 80, 78, 71, 13, 10, 26, 10))
BUTTON_H = 64
VR_RETRY_SECONDS = 5.0
EVENT_POLL_SECONDS = 0.05
VISIBLE_POLL_SECONDS = 0.25     # is_visible() while connected (hidden tab: no uploads)
REDRAW_SECONDS = 0.25           # visible tab: periodic redraw (4 Hz)
DIRTY_MIN_SECONDS = 0.04        # hover, click, tab shown: redraw at once, but at most 25 Hz
FILE_MIN_INTERVAL = 2.0         # file mode: SetOverlayFromFile at most every 2 s (SteamVR loads it in ~1 s)
RETRY_SECONDS = 5.0             # after a failed upload, also in standby
FAIL_RECONNECT_SECONDS = 30.0   # uploads failing this long (not standby): new SteamVR session
GL_STREAK = 3                   # consecutive GL errors before the GL context is rebuilt
GL_STREAK_WINDOW = 600.0        # a second streak within this time: GL is given up for the process
LIVENESS_SECONDS = 5.0          # is our overlay still the one SteamVR knows under OVERLAY_KEY?
CONFIRM_AFTER_SECONDS = 1.0     # ask for the texture size this long after the first upload
STATS_SECONDS = 30.0            # debug statistics and lock heartbeat
LOCK_ATTEMPTS = 10              # a starting instance tries the lock this often, LOCK_RETRY_SECONDS apart:
LOCK_RETRY_SECONDS = 0.1        # a probe (--status, --start, gl_exit_test.sh) holds it for a moment
STEAMVR_PROCESSES = ("vrcompositor", "vrserver")   # /proc/<pid>/comm of a running SteamVR (prefixes)
IP_CACHE_SECONDS = 30.0
FPS_HYSTERESIS = 5.0            # file mode: the shown frame rate changes only when it is this far off
NEVER = float("-inf")

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

# layout
CELL_W, CELL_H = fcam_font.CELL_W, fcam_font.CELL_H
CARD = (16, 16, PANEL_W - 16, PANEL_H - 16 - BUTTON_H - 12)   # status card; ends at y 308
LABEL_X = 32
VALUE_X = LABEL_X + 9 * CELL_W
VALUE_CHARS = (CARD[2] - 16 - VALUE_X) // CELL_W
LINE_CHARS = (CARD[2] - 16 - LABEL_X) // CELL_W
FOOTER_Y = 278                  # below the last row (254..277), inside the card

log = logging.getLogger("fcam.overlay")


# ----------------------------------------------------------------------------- software renderer

class Panel:
    """An RGBA8 raster the size of the overlay, drawn with the generated bitmap font."""

    _glyph_cache = {}

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

    @classmethod
    def glyph_rows_bytes(cls, ch, rgba, scale, bg):
        """Pre-rendered rows (bytes) of one glyph cell with the background baked in; one slice copy per row."""
        key = (ch, rgba, scale, bg)
        rows = cls._glyph_cache.get(key)
        if rows is None:
            on, off = bytes(rgba), bytes(bg)
            rows = []
            for row in fcam_font.glyph_rows(ch):
                line = b"".join((on if row >> rx & 1 else off) * scale for rx in range(CELL_W))
                rows.extend([line] * scale)
            cls._glyph_cache[key] = rows
        return rows

    def text(self, x, y, string, rgba=TEXT, scale=1, bg=PANEL):
        """Draws whole glyph cells, background included, so bg must be the colour under the text."""
        stride = self.width * 4
        glyph_w = CELL_W * scale
        for index, ch in enumerate(string):
            gx = x + index * glyph_w
            if gx < 0 or gx + glyph_w > self.width:
                break
            rows = self.glyph_rows_bytes(ch, rgba, scale, bg)
            for ry, line in enumerate(rows):
                yy = y + ry
                if 0 <= yy < self.height:
                    start = yy * stride + gx * 4
                    self.buf[start:start + glyph_w * 4] = line

    def text_width(self, string, scale=1):
        return len(string) * CELL_W * scale

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


def magnitude(count):
    """A counter as its order of magnitude: exact below 10, then 10+, 100+, 1k+, 10k+, 100k+, 1M+.
    Even a counter that grows with every frame changes it only a handful of times."""
    count = int(count)
    if count < 10:
        return str(count)
    power = len(str(count)) - 1
    for exponent, suffix in ((9, "G"), (6, "M"), (3, "k"), (0, "")):
        if power >= exponent:
            return "%d%s+" % (10 ** (power - exponent), suffix)


class CoarseNumbers:
    """File mode shows these instead of numbers that change with every frame, because every changed
    panel is a SteamVR file reload (a blink). The frame rate keeps its shown value (a multiple of 5)
    until the measured one is FPS_HYSTERESIS away from it, so a rate hovering around a rounding
    boundary does not flip; counters show only their magnitude; the uptime becomes the time the
    bridge started, which does not tick at all."""

    def __init__(self, wall=time.time):
        self.shown_fps = None
        self.wall = wall
        self.since_text = None
        self.last_uptime = None

    def fps(self, fps):
        if self.shown_fps is None or abs(fps - self.shown_fps) >= FPS_HYSTERESIS:
            self.shown_fps = 5 * int(round(fps / 5.0))
        return "~%d fps" % self.shown_fps if self.shown_fps else "0 fps"

    def since(self, uptime):
        """'since HH:MM', the local time the bridge started. Worked out once (wall clock minus uptime
        jitters by a second) and again only when the uptime went back: the bridge was restarted."""
        if self.since_text is None or uptime < self.last_uptime:
            self.since_text = time.strftime("%H:%M", time.localtime(self.wall() - uptime))
        self.last_uptime = uptime
        return "since " + self.since_text


def draw_panel(panel, snapshot, hot_button, vr_version, sink_label="", ip=None, coarse=None, debug_frame=None):
    """Renders the status panel. coarse (a CoarseNumbers, file mode) replaces the numbers that change
    with every frame; debug_frame (FCAM_DEBUG_FORCE_UPLOAD_HZ) adds a moving marker and a frame counter."""
    panel.fill(BG)
    cx0, cy0, cx1, cy1 = CARD
    panel.fill(PANEL, cx0, cy0, cx1, cy1)

    panel.text(LABEL_X, 28, "FCAM Bridge", ACCENT, scale=2, bg=PANEL)
    state = snapshot.get("state", "?")
    state_color = {"streaming": OK, "opening": WARN, "no-source": BAD, "stopping": DIM}.get(state, DIM)
    state_text = {"streaming": "tracker streaming", "opening": "opening tracker",
                  "no-source": "no tracker (" + snapshot.get("source", "?") + ")", "stopping": "stopping"}.get(state, state)
    panel.text(LABEL_X, 90, state_text[:LINE_CHARS], state_color, bg=PANEL)

    fps, frames, bad = snapshot.get("fps", 0.0), snapshot.get("frames", 0), snapshot.get("bad", 0)
    dropped, unsent, errors = snapshot.get("dropped", 0), snapshot.get("unsent", 0), snapshot.get("send_errors", 0)
    uptime, opens = snapshot.get("uptime", 0), snapshot.get("opens", 0)
    if coarse is not None:
        tracker = "%s, %s bad" % (coarse.fps(fps), magnitude(bad))
        if snapshot.get("subscribers") or snapshot.get("targets"):
            drops = "%s (queue), %s unsent, %s send errors" % (magnitude(dropped), magnitude(unsent),
                                                                magnitude(errors))
        else:  # frames nobody receives count up as "unsent" with every frame: not shown while idle
            drops = "%s (queue), %s send errors" % (magnitude(dropped), magnitude(errors))
        running = "%s, port opened %d time(s)" % (coarse.since(uptime), opens)
    else:
        tracker = "%.1f fps, %d frames, %d bad" % (fps, frames, bad)
        drops = "%d (queue), %d unsent, %d send errors" % (dropped, unsent, errors)
        running = "%ds, port opened %d time(s)" % (uptime, opens)
    rows = [
        ("address", "fcam://%s:%d" % (ip or local_ip(), snapshot.get("listen_port", fcam_bridge.DEFAULT_PORT))),
        ("tracker", tracker),
        ("dropped", drops),
        ("clients", ", ".join(snapshot.get("subscribers", [])) or "none (start the face camera in Baballonia)"),
        ("uptime", running),
    ]
    y = 130
    for label, value in rows:
        panel.text(LABEL_X, y, label.rjust(8), DIM, bg=PANEL)
        panel.text(VALUE_X, y, value[:VALUE_CHARS], TEXT, bg=PANEL)
        y += CELL_H + 8

    footer = "SteamVR %s" % (vr_version or "?")
    if sink_label:
        footer += "  " + sink_label
    panel.text(LABEL_X, FOOTER_Y, footer[:LINE_CHARS], DIM, bg=PANEL)

    if debug_frame is not None:
        counter = "#%d" % debug_frame
        panel.text(cx1 - 16 - panel.text_width(counter), FOOTER_Y, counter, WARN, bg=PANEL)
        track_x0, track_x1 = 320, cx1 - 32
        mx = track_x0 + (debug_frame * 8) % (track_x1 - track_x0)
        panel.fill(WARN, mx, 36, mx + 16, 52)

    bx0, by0, bx1, by1 = 16, PANEL_H - 16 - BUTTON_H, PANEL_W - 16, PANEL_H - 16
    button = BUTTON_HOT if hot_button else BUTTON
    panel.fill(button, bx0, by0, bx1, by1)
    panel.outline(ACCENT, bx0, by0, bx1, by1)
    label = "Restart bridge"
    panel.text((PANEL_W - panel.text_width(label, 2)) // 2, by0 + (BUTTON_H - CELL_H * 2) // 2, label,
               TEXT, scale=2, bg=button)


def in_button(x, y):
    """Hit test for the restart button. Mouse y may be top-down or bottom-up depending on runtime
    conventions, so the bottom band is accepted in both orientations; nothing else sits opposite it."""
    if not 0 <= x <= PANEL_W:
        return False
    band = BUTTON_H + 16
    return y <= band or y >= PANEL_H - band


# ----------------------------------------------------------------------------- texture sinks

class TextureSink:
    """How the panel raster reaches SteamVR. submit() raises on failure (OpenVRError, GlError, OSError)
    and returns False when there was nothing new to hand over."""

    mode = "none"          # --texture-mode this sink implements; logged as "(<mode> mode)"
    label = "none"         # shown in the footer and the logs
    uploads = True         # False: nothing is ever drawn or uploaded
    live = True            # False: draw coarse numbers (every change costs a reload)
    min_interval = 0.0     # minimum seconds between two uploads
    timings = (0.0, 0.0)   # (prepare, hand-over) seconds of the last upload

    def connected(self):
        """Called after every successful connect (a new overlay without a texture)."""

    def before_teardown(self):
        """Called before the overlay is cleared and destroyed."""

    def submit(self, overlay, handle, panel):
        raise NotImplementedError

    def close(self):
        """Releases what the sink holds. Called with no SteamVR session referring to it."""


class GlSink(TextureSink):
    """One persistent GLES texture handed to SteamVR with SetOverlayTexture on every update."""

    mode = "gl"
    label = "GL"

    def __init__(self, gl):
        self.gl = gl
        self.info = getattr(gl, "info", "?")

    def submit(self, overlay, handle, panel):
        start = time.perf_counter()
        self.gl.upload(panel.buf)
        uploaded = time.perf_counter()
        overlay.set_gl_texture(handle, self.gl.texture, ovr.ColorSpace_Gamma)
        self.timings = (uploaded - start, time.perf_counter() - uploaded)
        return True

    def before_teardown(self):
        self.gl.finish()  # SteamVR's copy of the last upload is complete before the overlay goes away

    def close(self):
        gl, self.gl = self.gl, None
        if gl is not None:
            gl.close(terminate=False)


def panel_file_dir():
    """Where file mode writes its PNGs: $XDG_RUNTIME_DIR (a per-user tmpfs; SteamVR runs as the same
    user), else /dev/shm, else the temp directory."""
    for path in (os.environ.get("XDG_RUNTIME_DIR"), "/dev/shm"):
        if path and os.path.isdir(path) and os.access(path, os.W_OK):
            return path
    return tempfile.gettempdir()


class FileSink(TextureSink):
    """PNG files loaded by SteamVR itself (SetOverlayFromFile). SetOverlayRaw would leave the compositor
    holding a client-owned buffer, and vrcompositor on the Steam Frame (SteamVR 2.17.10) crashes when
    that client exits. The load is asynchronous, so uploads are rare: only changed content, at most
    every min_interval seconds (enforced by the caller), alternating two file names so every call
    names a new file."""

    mode = "file"
    live = False

    def __init__(self, directory=None, label="file", min_interval=None):
        self.directory = directory or panel_file_dir()
        self.label = label
        self.min_interval = FILE_MIN_INTERVAL if min_interval is None else float(min_interval)
        base = os.path.join(self.directory, "fcam-panel-%d" % os.getpid())
        self.paths = (base + "-a.png", base + "-b.png")
        self.next_index = 0
        self.last_crc = None

    def connected(self):
        self.last_crc = None  # a new overlay has no texture yet

    def submit(self, overlay, handle, panel):
        crc = zlib.crc32(panel.buf)
        if crc == self.last_crc:
            return False
        path = self.paths[self.next_index]
        start = time.perf_counter()
        self._write(path, panel.to_png())
        written = time.perf_counter()
        overlay.set_from_file(handle, path)
        self.timings = (written - start, time.perf_counter() - written)
        self.last_crc = crc
        self.next_index ^= 1
        return True

    def _write(self, path, data):
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=os.path.basename(path)[:-len(".png")] + ".",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def close(self):
        for path in self.paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as e:
                log.debug("cannot remove %s: %s", path, e)


class NoneSink(TextureSink):
    """No texture at all: the tab stays blank, the thumbnail and the log remain."""

    uploads = False

    def __init__(self, label="none"):
        self.label = label

    def submit(self, overlay, handle, panel):
        return False


class RawSink(TextureSink):
    """SetOverlayRaw. Only with --unsafe-raw: vrcompositor on the Steam Frame crashes when we exit."""

    mode = "raw"
    label = "RAW UNSAFE"

    def connected(self):
        log.warning("texture mode raw (--unsafe-raw): SetOverlayRaw; vrcompositor on the Steam Frame crashes "
                    "(SIGBUS) when this process exits")

    def submit(self, overlay, handle, panel):
        start = time.perf_counter()
        overlay.set_raw(handle, panel.buf, panel.width, panel.height)
        self.timings = (0.0, time.perf_counter() - start)
        return True


def default_gl_factory():
    if gl_texture is None:
        raise RuntimeError("gl_texture.py is not installed next to fcam_overlay.py")
    return gl_texture.GlTexture(PANEL_W, PANEL_H)


def describe(error):
    return "%s: %s" % (type(error).__name__, error)


def gl_fallback(mode, reason, file_factory=None):
    """The sink that replaces GL: file for auto, nothing for the strict gl mode (a failed test shows)."""
    if mode == "auto":
        log.error("GL texture path unavailable (%s); using SetOverlayFromFile", reason)
        return (file_factory or FileSink)(label="file (GL failed)")
    log.error("GL texture path unavailable (%s); --texture-mode gl has no fallback, the tab stays blank", reason)
    return NoneSink(label="none (GL failed)")


def resolve_sink(mode, gl_factory=None, file_factory=None, unsafe_raw=False):
    """The texture sink for --texture-mode. GL failures fall back to file (auto) or none (gl);
    SetOverlayRaw is only ever used when asked for with --unsafe-raw."""
    file_factory = file_factory or FileSink
    if mode == "raw":
        if not unsafe_raw:
            raise ValueError(RAW_REFUSED)
        return RawSink()
    if mode == "none":
        return NoneSink()
    if mode == "file":
        return file_factory()
    if mode not in ("auto", "gl"):
        raise ValueError("unknown texture mode %r" % mode)
    try:
        gl = (gl_factory or default_gl_factory)()
    except Exception as e:  # GlError, OSError, RuntimeError (no gl_texture.py), ctypes errors
        return gl_fallback(mode, describe(e), file_factory)
    sink = GlSink(gl)
    log.info("GL texture path: %s", sink.info)
    return sink


# ----------------------------------------------------------------------------- upload errors

STANDBY_CODES = (ovr.VROverlayError_RequestFailed, ovr.VROverlayError_TimedOut)
RECONNECT_CODES = (ovr.VROverlayError_UnknownOverlay, ovr.VROverlayError_InvalidHandle)
GL_REJECTED_CODES = (ovr.VROverlayError_InvalidTexture,)   # SteamVR does not take our GL texture
UPLOAD_ERRORS = (ovr.OpenVRError, OSError) + ((gl_texture.GlError,) if gl_texture is not None else ())


class ReconnectNeeded(Exception):
    """The SteamVR session (or our overlay in it) has to be rebuilt."""


def classify_upload_error(error, gl_sink=False):
    """'standby' (compositor suspended: retry later), 'reconnect' (the overlay is gone), 'gl' (our GL
    context, or SteamVR rejecting the GL texture of a GlSink) or 'other', for an exception from
    TextureSink.submit()."""
    if gl_texture is not None and isinstance(error, gl_texture.GlError):
        return "gl"
    if isinstance(error, ovr.OpenVRError):
        if error.code in STANDBY_CODES:
            return "standby"
        if error.code in RECONNECT_CODES:
            return "reconnect"
        if gl_sink and error.code in GL_REJECTED_CODES:
            return "gl"
    return "other"


class UploadStats:
    """What the debug statistics line reports every STATS_SECONDS."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.uploads = self.unchanged = self.failures = 0
        self.draw, self.prepare, self.handover = [], [], []

    @staticmethod
    def _ms(values):
        if not values:
            return "-"
        ordered = sorted(values)
        return "p50 %.1f max %.1f ms" % (ordered[len(ordered) // 2] * 1000.0, ordered[-1] * 1000.0)

    def summary(self, label, visible):
        return ("sink %s: %d uploads, %d unchanged, %d failed; draw %s; upload %s; set-texture %s; tab %s"
                % (label, self.uploads, self.unchanged, self.failures, self._ms(self.draw), self._ms(self.prepare),
                   self._ms(self.handover), "visible" if visible else "hidden"))


def env_float(env, name):
    """A positive number from a debugging environment variable, or None."""
    text = env.get(name)
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        log.warning("ignoring %s=%r (not a number)", name, text)
        return None
    return value if value > 0 else None


# ----------------------------------------------------------------------------- overlay app

class OverlayApp:
    """Every OpenVR and GL call happens on the thread that calls run() (the main thread). The bridge
    runs on its own thread and is only read through snapshot()."""

    def __init__(self, args, runtime=None, runtime_factory=None, session_factory=None, gl_factory=None,
                 file_factory=None, clock=time.monotonic, env=None, lock=None):
        self.args = args
        self.env = os.environ if env is None else env
        self.clock = clock
        self.lock = lock
        self.runtime_factory = runtime_factory or fcam_bridge.BridgeRuntime
        self.runtime = runtime if runtime is not None else self.runtime_factory(args)
        self.session_factory = session_factory or self._open_session
        self.gl_factory = gl_factory or default_gl_factory
        file_interval = env_float(self.env, "FCAM_DEBUG_FILE_INTERVAL")
        self.file_factory = file_factory or (lambda label="file": FileSink(label=label, min_interval=file_interval))
        self.texture_mode = args.texture_mode
        if self.env.get("FCAM_DEBUG_NO_TEXTURE"):
            self.texture_mode = "none"  # older name of --texture-mode none
        force_hz = env_float(self.env, "FCAM_DEBUG_FORCE_UPLOAD_HZ")
        self.force_interval = 1.0 / force_hz if force_hz else None
        self.reconnect_every = env_float(self.env, "FCAM_DEBUG_RECONNECT_EVERY")
        self.ip_provider = local_ip
        self.session = None
        self.main_handle = None
        self.thumb_handle = None
        self.sink = None
        self.panel = Panel(PANEL_W, PANEL_H)
        self.coarse = CoarseNumbers()   # what a sink that is not live (file mode) shows
        self.stop = threading.Event()
        self.hot_button = False
        self.last_vr_attempt = NEVER
        self.vr_error_logged = None
        self.vr_version = ""
        self.ip, self.ip_at = None, NEVER
        self.last_upload = NEVER        # kept across reconnects: file mode keeps its spacing
        self.last_draw = NEVER
        self.failed_uploads = 0
        self.failing_since = None       # first failure of the current episode, for "work again"
        self.gl_streak = 0
        self.gl_rebuilt_at = None
        self.gl_handover_ok = False     # SteamVR took a GL texture from this process at least once
        self.gl_failed_sessions = 0     # sessions ended by GL hand-overs failing with an 'other' error
        self.debug_frame = 0
        self.stats = UploadStats()
        self.next_stats = clock() + STATS_SECONDS
        self.last_error_key, self.last_error_at = None, NEVER
        self.reset_connection_state(clock())
        self.lock_update(texture_mode=self.texture_mode)

    def reset_connection_state(self, now):
        self.visible = False
        self.dirty = True
        self.initial_pending = True     # one upload right after connecting, even with the tab hidden
        self.retry_at = NEVER
        self.fail_since = None          # non-standby failure streak of this session
        self.standby_logged = False
        self.liveness_standby_logged = False
        self.texture_confirmed = False
        self.texture_warned = False
        self.first_ok_at = None
        self.next_confirm_at = NEVER
        self.connected_at = now
        self.next_visible_check = now
        self.next_liveness_check = now + LIVENESS_SECONDS

    def lock_update(self, **fields):
        if self.lock is not None:
            try:
                self.lock.update(**fields)
            except OSError as e:
                log.debug("lock update: %s", e)

    def local_ip(self, now):
        if self.ip is None or now - self.ip_at >= IP_CACHE_SECONDS:
            self.ip, self.ip_at = self.ip_provider(), now
        return self.ip

    # -- texture sink

    def make_sink(self):
        sink = resolve_sink(self.texture_mode, self.gl_factory, self.file_factory, unsafe_raw=self.args.unsafe_raw)
        self.lock_update(sink=sink.label)
        return sink

    def close_sink(self):
        sink, self.sink = self.sink, None
        if sink is not None:
            try:
                sink.close()
            except Exception as e:
                log.warning("closing the %s texture path: %s", sink.label, e)

    # -- SteamVR connection

    def _open_session(self, app_type):
        return ovr.Session(self.args.openvr_lib).init(app_type)

    def connect_vr(self):
        now = self.clock()
        if now - self.last_vr_attempt < VR_RETRY_SECONDS:
            return False
        self.last_vr_attempt = now
        app_type = ovr.VRApplication_Background if self.args.app_type == "background" else ovr.VRApplication_Overlay
        try:
            session = self.session_factory(app_type)
        except ovr.OpenVRError as e:
            message = str(e)
            if message != self.vr_error_logged:
                if e.code is None:   # not a VR_Init error: no runtime library, or a pinned interface missing
                    log.error("SteamVR runtime not usable: %s", message)
                else:
                    log.info("SteamVR not available yet: %s", message)
                self.vr_error_logged = message
            return False
        self.vr_error_logged = None
        try:
            if self.sink is None:
                self.sink = self.make_sink()  # GL context: created here, on this thread, before any overlay
            overlay = session.overlay
            existing = overlay.find_overlay(OVERLAY_KEY)
            if existing:
                log.debug("DestroyOverlay(existing %#x): %s", existing, overlay.destroy_overlay(existing))
            main, thumb = overlay.create_dashboard_overlay(OVERLAY_KEY, OVERLAY_NAME)
            overlay.set_width_meters(main, 1.5)
            overlay.set_input_method(main, ovr.VROverlayInputMethod_Mouse)
            overlay.set_mouse_scale(main, PANEL_W, PANEL_H)
            if os.path.exists(ICON) and not self.env.get("FCAM_DEBUG_NO_THUMB"):
                try:
                    overlay.set_from_file(thumb, ICON)
                except ovr.OpenVRError as e:
                    log.warning("thumbnail not set: %s", e)
            try:
                session.applications.identify(os.getpid(), APP_KEY)
            except ovr.OpenVRError as e:
                log.debug("IdentifyApplication: %s", e)
            self.vr_version = session.system.runtime_version()
        except ovr.OpenVRError as e:
            log.error("overlay setup failed: %s", e)
            session.shutdown()
            return False
        except BaseException:
            session.shutdown()
            raise
        self.session = session
        self.main_handle, self.thumb_handle = main, thumb
        self.reset_connection_state(now)
        self.sink.connected()
        log.info("SteamVR connected (%s, %s, %s, runtime %s, %s), texture: %s", session.system.version,
                 session.overlay.version, session.applications.version, self.vr_version, session.library_path,
                 self.sink.label)
        return True

    def disconnect_vr(self):
        """Teardown order: glFinish, ClearOverlayTexture, DestroyOverlay, VR_Shutdown. The GL context
        stays alive (and current) for the next session; close_sink() releases it at exit."""
        session = self.session
        if session is None:
            return
        main = self.main_handle
        self.session = None
        self.main_handle = self.thumb_handle = None
        # FCAM_DEBUG_EXIT_MODE: "clear" clears the texture before destroying the overlay (default),
        # "destroy" destroys without clearing, "nodestroy" only shuts the session down,
        # "clear-nodestroy" clears without destroying; a "-sleep" suffix waits 1 s before VR_Shutdown.
        mode = self.env.get("FCAM_DEBUG_EXIT_MODE", "clear")
        if self.sink is not None:
            try:
                self.sink.before_teardown()
            except Exception as e:
                log.warning("finishing the %s texture path: %s", self.sink.label, e)
        codes = []
        if main and mode.startswith("clear"):
            codes.append("ClearOverlayTexture %s" % self._teardown_call(session.overlay.clear_texture, main))
        if main and "nodestroy" not in mode:
            codes.append("DestroyOverlay %s" % self._teardown_call(session.overlay.destroy_overlay, main))
        if mode.endswith("sleep"):
            time.sleep(1.0)
        try:
            session.shutdown()
        except Exception as e:
            log.warning("VR_Shutdown: %s", e)
        log.info("SteamVR session closed (%s)", ", ".join(codes) or "overlay kept: FCAM_DEBUG_EXIT_MODE=%s" % mode)

    @staticmethod
    def _teardown_call(fn, handle):
        try:
            return fn(handle)
        except Exception as e:
            return describe(e)

    # -- drawing and upload

    def next_upload_time(self):
        """Monotonic time the next draw and upload are due, or None while none is due."""
        sink = self.sink
        if self.session is None or sink is None or not sink.uploads:
            return None
        earliest = max(self.retry_at, self.last_upload + sink.min_interval)
        if self.force_interval:
            return max(earliest, self.last_draw + self.force_interval)
        if self.initial_pending:
            return earliest
        if not self.visible:
            return None
        return max(earliest, self.last_draw + (DIRTY_MIN_SECONDS if self.dirty else REDRAW_SECONDS))

    def redraw(self, now):
        """Draws the panel and hands it to the sink. Returns True when SteamVR got new content."""
        sink = self.sink
        start = time.perf_counter()
        snapshot = self.runtime.snapshot()
        if self.force_interval:
            self.debug_frame += 1
        draw_panel(self.panel, snapshot, self.hot_button, self.vr_version, sink.label, self.local_ip(now),
                   coarse=None if sink.live else self.coarse,
                   debug_frame=self.debug_frame if self.force_interval else None)
        self.stats.draw.append(time.perf_counter() - start)
        self.last_draw = now
        try:
            sent = sink.submit(self.session.overlay, self.main_handle, self.panel)
        except UPLOAD_ERRORS as e:
            self.last_upload = now
            self.upload_failed(e, now)
            return False
        self.dirty = False
        self.initial_pending = False
        if sent:
            self.last_upload = now
            self.stats.uploads += 1
            if isinstance(sink, GlSink):
                self.gl_handover_ok = True
            prepare, handover = sink.timings
            self.stats.prepare.append(prepare)
            self.stats.handover.append(handover)
            if self.first_ok_at is None:
                self.first_ok_at = now
                self.next_confirm_at = now + CONFIRM_AFTER_SECONDS
        else:
            self.stats.unchanged += 1
        self.upload_ok(now)
        return sent

    def upload_ok(self, now):
        if self.failing_since is not None:
            log.info("overlay uploads work again after %.0f s (%d failed)", now - self.failing_since,
                     self.failed_uploads)
        self.failing_since = None
        self.failed_uploads = 0
        self.fail_since = None
        self.standby_logged = False
        self.gl_streak = 0
        self.retry_at = NEVER

    def upload_failed(self, error, now):
        """standby: retry in 5 s; reconnect: ReconnectNeeded; other: retry in 5 s, new session after
        30 s; GL: after GL_STREAK in a row, rebuild the GL context (once), then give GL up. With a GL
        sink that SteamVR never took a texture from, an 'other' error that outlasts one new session
        counts as a GL failure too, so auto still ends in file mode."""
        gl_sink = isinstance(self.sink, GlSink)
        kind = classify_upload_error(error, gl_sink=gl_sink)
        gl_suspect = gl_sink and not self.gl_handover_ok
        if kind == "other" and gl_suspect and self.gl_failed_sessions >= 1:
            kind = "gl"
        self.stats.failures += 1
        self.failed_uploads += 1
        if self.failing_since is None:
            self.failing_since = now
        self.retry_at = now + RETRY_SECONDS
        if kind == "reconnect":
            raise ReconnectNeeded("overlay upload failed (%s)" % error)
        if kind == "standby":
            if not self.standby_logged:
                self.standby_logged = True
                log.warning("overlay upload failed (%s); compositor in standby? retrying every %.0f s",
                            error, RETRY_SECONDS)
            return
        if self.fail_since is None:
            self.fail_since = now
            log.warning("overlay upload failed (%s); retrying every %.0f s", error, RETRY_SECONDS)
        else:
            log.debug("overlay upload failed again (%s)", error)
        if kind == "gl":
            self.gl_streak += 1
            if self.gl_streak >= GL_STREAK:
                self.gl_streak_exceeded(now, error)
            return
        if now - self.fail_since >= FAIL_RECONNECT_SECONDS:
            if gl_suspect:
                self.gl_failed_sessions += 1
            raise ReconnectNeeded("overlay uploads failing for %.0f s (%s)" % (now - self.fail_since, error))

    def gl_streak_exceeded(self, now, error):
        """Rebuilds the GL context once with no VR session open (disconnect, close GL, create GL; the
        loop reconnects). A second streak within GL_STREAK_WINDOW gives GL up for the process."""
        second = self.gl_rebuilt_at is not None and now - self.gl_rebuilt_at < GL_STREAK_WINDOW
        self.gl_streak = 0
        log.error("%d GL uploads failed in a row (%s); %s", GL_STREAK, error,
                  "again within %.0f min" % (GL_STREAK_WINDOW / 60) if second else "rebuilding the GL context")
        self.disconnect_vr()
        self.close_sink()
        if second:
            self.sink = gl_fallback(self.texture_mode, "GL uploads keep failing: %s" % error, self.file_factory)
            self.lock_update(sink=self.sink.label)
        else:
            self.gl_rebuilt_at = now
            self.sink = self.make_sink()

    def confirm_texture(self, now):
        """Logs the texture size once SteamVR has it (SetOverlayFromFile loads asynchronously)."""
        if self.texture_confirmed or self.first_ok_at is None or self.session is None or now < self.next_confirm_at:
            return
        self.next_confirm_at = now + 0.5
        age = now - self.first_ok_at
        try:
            width, height = self.session.overlay.texture_size(self.main_handle)
        except ovr.OpenVRError as e:
            if age > 10.0 and not self.texture_warned:
                log.warning("panel texture still not confirmed after %.0f s: %s", age, e)
                self.texture_warned = True
            return
        log.info("panel texture %dx%d set (%s mode)", width, height, self.sink.mode)
        self.texture_confirmed = True

    # -- input

    def restart_bridge(self):
        log.info("restarting bridge on request")
        try:
            self.runtime.stop()
            time.sleep(0.5)  # let the old socket close before binding the same port again
            self.runtime = self.runtime_factory(self.args)
            self.runtime.start()
        except Exception:
            log.exception("bridge restart failed")

    def set_hot(self, hot):
        if hot != self.hot_button:
            self.hot_button = hot
            self.dirty = True

    def set_visible(self, visible):
        if visible != self.visible:
            log.debug("tab %s", "visible" if visible else "hidden")
            self.visible = visible
            if visible:
                self.dirty = True

    def handle_events(self):
        """Returns False when SteamVR asked us to quit. Input only marks the panel dirty."""
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
            event_type = event.eventType
            if event_type == ovr.VREvent_MouseMove:
                x, y, _ = event.mouse()
                self.set_hot(in_button(x, y))
            elif event_type == ovr.VREvent_MouseButtonDown:
                x, y, _ = event.mouse()
                if in_button(x, y):
                    self.restart_bridge()
                    self.dirty = True
            elif event_type == ovr.VREvent_OverlayShown:
                self.set_visible(True)
                self.dirty = True
            elif event_type == ovr.VREvent_OverlayHidden:
                self.set_hot(False)
                self.set_visible(False)
            elif event_type == ovr.VREvent_FocusLeave:
                self.set_hot(False)
        return True

    # -- main loop

    def step(self, now):
        """One pass while connected. Returns False when SteamVR asked us to quit; raises
        ReconnectNeeded or OpenVRError when the session has to be rebuilt."""
        if not self.handle_events():
            return False
        if self.reconnect_every and now - self.connected_at >= self.reconnect_every:
            raise ReconnectNeeded("FCAM_DEBUG_RECONNECT_EVERY=%g" % self.reconnect_every)
        if now >= self.next_visible_check:
            self.next_visible_check = now + VISIBLE_POLL_SECONDS
            self.set_visible(self.session.overlay.is_visible(self.main_handle))
        if now >= self.next_liveness_check:
            self.next_liveness_check = now + LIVENESS_SECONDS
            self.check_liveness()
        due = self.next_upload_time()
        if due is not None and now >= due:
            self.redraw(now)
        if self.session is not None:  # a GL rebuild disconnects
            self.confirm_texture(now)
        return True

    def check_liveness(self):
        """Raises ReconnectNeeded when SteamVR no longer knows our overlay under OVERLAY_KEY. The error
        codes count as for uploads: a compositor in standby (RequestFailed, TimedOut) is not a lost
        overlay, and neither is any other error; only UnknownOverlay/InvalidHandle or another handle is."""
        code, found = self.session.overlay.find_overlay_code(OVERLAY_KEY)
        if code in STANDBY_CODES:
            if not self.liveness_standby_logged:
                self.liveness_standby_logged = True
                log.debug("liveness probe: FindOverlay answered %d; compositor in standby? not reconnecting", code)
            return
        self.liveness_standby_logged = False
        if code in RECONNECT_CODES or (code == 0 and found != self.main_handle):
            raise ReconnectNeeded("SteamVR no longer knows our overlay (FindOverlay %s: %d, handle %s, ours %s)"
                                  % (OVERLAY_KEY, code, found, self.main_handle))
        if code != 0:
            log.debug("liveness probe: FindOverlay answered %d; not reconnecting", code)

    def sleep_time(self, now):
        due = self.next_upload_time()
        if due is None:
            return EVENT_POLL_SECONDS
        return min(EVENT_POLL_SECONDS, max(0.002, due - now))

    def periodic(self, now):
        """Debug statistics and the lock heartbeat, every STATS_SECONDS."""
        if now < self.next_stats:
            return
        self.next_stats = now + STATS_SECONDS
        label = self.sink.label if self.sink is not None else "-"
        if self.session is not None:
            log.debug("stats: %s", self.stats.summary(label, self.visible))
        self.stats.reset()
        self.lock_update(heartbeat=int(time.time()), sink=label)

    def report_exception(self):
        """Logs the exception being handled; a repeat of the same error is logged again after 60 s."""
        error = sys.exc_info()[1]
        key = describe(error)
        now = self.clock()
        if key != self.last_error_key or now - self.last_error_at >= 60.0:
            log.exception("overlay error (the bridge keeps running; retrying in 1 s)")
            self.last_error_key, self.last_error_at = key, now
        else:
            log.debug("overlay error again: %s", key)

    def shutdown(self):
        """Process exit, in this order: SteamVR teardown (glFinish, clear, destroy, VR_Shutdown), the
        texture path (GL context closed without eglTerminate, panel files removed), then the bridge."""
        for what, step in (("SteamVR teardown", self.disconnect_vr), ("texture path", self.close_sink),
                           ("bridge", self.runtime.stop)):
            try:
                step()
            except Exception:
                log.exception("%s failed during shutdown", what)

    def run(self):
        log.info("FCAM overlay %s starting (pid %d, texture mode %s)", fcam_bridge.__version__, os.getpid(),
                 self.texture_mode)
        self.runtime.start()
        try:
            while not self.stop.is_set():
                delay = EVENT_POLL_SECONDS
                try:
                    self.periodic(self.clock())
                    if self.session is None:
                        self.connect_vr()
                        self.stop.wait(0.5 if self.session is None else 0)
                        continue
                    if not self.step(self.clock()):
                        break
                    delay = self.sleep_time(self.clock())
                except ReconnectNeeded as e:
                    log.warning("reconnecting to SteamVR: %s", e)
                    self.disconnect_vr()
                    continue
                except ovr.OpenVRError as e:
                    log.warning("SteamVR call failed, reconnecting: %s", e)
                    self.disconnect_vr()
                    continue
                except Exception:
                    self.report_exception()
                    delay = 1.0
                self.stop.wait(delay)
        finally:
            self.shutdown()
            log.info("FCAM overlay stopped")


# ----------------------------------------------------------------------------- single instance

def default_lock_path():
    """$XDG_RUNTIME_DIR/ottlabs.fcam.lock: one instance per user, whichever directory it runs from."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base and hasattr(os, "getuid"):
        base = "/run/user/%d" % os.getuid()
    if not base or not os.path.isdir(base):
        base = HERE
    return os.path.join(base, LOCK_NAME)


def lock_text(fields):
    return json.dumps(fields, sort_keys=True)


def parse_lock(text):
    """Contents of a lock file: the JSON object of this version, or the plain pid 0.1.x wrote."""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return {"pid": int(text), "legacy": True}
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("pid"), int):
        return data
    return None


class InstanceLock:
    """The single-instance lock: flock on a file holding JSON {pid, version, texture_mode, sink, heartbeat}."""

    def __init__(self, path):
        self.path = path
        self.handle = None
        self.fields = {}

    def acquire(self, **fields):
        """True when we hold the lock now, False when another instance does."""
        self.fields.update(fields)
        if fcntl is None:  # not Linux (development only)
            return True
        handle = open(self.path, "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        self._write()
        return True

    def update(self, **fields):
        self.fields.update(fields)
        self._write()

    def _write(self):
        if self.handle is None:
            return
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(lock_text(self.fields))
        self.handle.flush()

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def probe_lock(path):
    """The contents of a lock file another process holds, or None when nobody holds it. The probe
    takes a shared lock for a moment: probes never collide with each other, and an instance that
    starts meanwhile tries its exclusive lock again (acquire_instance_lock)."""
    if fcntl is None or not os.path.exists(path):
        return None
    try:
        with open(path) as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return parse_lock(handle.read()) or {"pid": 0}
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass
    return None


def acquire_instance_lock(lock, fields, attempts=None, sleep=None):
    """True once lock is ours (holding fields). A probe holds the lock for a moment, so a failed attempt
    is repeated for about a second; only a running instance keeps it that long."""
    attempts = attempts or LOCK_ATTEMPTS
    for attempt in range(attempts):
        if lock.acquire(**fields):
            return True
        if attempt + 1 < attempts:
            (sleep or time.sleep)(LOCK_RETRY_SECONDS)
    return False


def running_instance(lock_path=None):
    """{pid, texture_mode, legacy, lock, ...} of the running instance, or None. Also finds a 0.1.x
    instance, which holds a plain-pid lock in its install directory."""
    for path, legacy in ((lock_path or default_lock_path(), False), (LEGACY_LOCK, True)):
        info = probe_lock(path)
        if info is not None:
            info = dict(info)
            info["legacy"] = legacy or bool(info.get("legacy"))
            info["lock"] = path
            return info
    return None


def running_pid():
    """Pid of the running instance, or 0 (kept for scripts written against 0.1.x)."""
    info = running_instance()
    return int(info.get("pid") or 0) if info else 0


def texture_mode_from_argv(argv, default):
    """The --texture-mode a command line selects (argparse also accepts unique prefixes such as
    --texture raw, and the last occurrence wins); default when it has none."""
    mode = default
    for index, token in enumerate(argv):
        name, sep, value = token.partition("=")
        if len(name) >= 4 and "--texture-mode".startswith(name):
            if not sep:
                value = argv[index + 1] if index + 1 < len(argv) else ""
            mode = value or mode
    return mode


def process_cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def process_alive(pid):
    """True while pid exists and has not exited (a zombie waiting for its parent counts as exited)."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            stat = fh.read()
    except OSError:
        return False
    end = stat.rfind(b")")
    return stat[end + 2:end + 3] not in (b"Z", b"X", b"x")


def is_overlay_cmdline(argv):
    return any(os.path.basename(arg) == "fcam_overlay.py" for arg in argv or ())


def is_overlay_pid(pid, info=None):
    """May pid be signalled as the overlay? Yes when it comes from this version's JSON lock; any
    other pid (a plain-pid 0.1.x lock, or what SteamVR reports) only while it runs fcam_overlay.py."""
    if not pid:
        return False
    if info is not None and not info.get("legacy") and int(info.get("pid") or 0) == pid:
        return True
    return is_overlay_cmdline(process_cmdline(pid))


def instance_texture_mode(info):
    """Texture mode of a running instance, or None when unknown. From its JSON lock; for any other
    instance (a 0.1.x one with a plain-pid lock, or a pid SteamVR reported) only from positive
    evidence, and only if the process is fcam_overlay.py at all: an explicit --texture-mode on its
    command line, or the panel file that only the 0.1.x builds in file mode write. A bare command
    line proves nothing: the first 0.1.0 builds had no --texture-mode and always used SetOverlayRaw."""
    if not info.get("legacy") and info.get("texture_mode"):
        return info["texture_mode"]
    pid = int(info.get("pid") or 0)
    argv = process_cmdline(pid) if pid else None
    if not is_overlay_cmdline(argv):
        return None
    mode = texture_mode_from_argv(argv, None)
    if mode is None and os.path.exists(LEGACY_PANEL_FILE % pid):
        mode = "file"
    return mode


def stop_pid(pid, kill=None):
    """SIGTERM to pid, as text for a status line; a pid that is gone already or not ours is no error."""
    try:
        (kill or os.kill)(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "pid %d had already exited" % pid
    except PermissionError as e:
        return "cannot stop pid %d (%s)" % (pid, e)
    return "stopped pid %d" % pid


def stop_instance(info, timeout=5.0, kill=None, alive=None, sleep=time.sleep, clock=time.monotonic):
    """SIGTERM to a running instance, then waits until it exited. Returns (outcome, text): "stopped" or
    "exited" (it is gone), "timeout" (still running) or "left" (not signalled: its pid is unknown, it
    may use raw mode, or the signal was refused). An instance in raw mode, or whose mode is unknown, is
    never signalled: stopping it while SteamVR runs crashes the compositor."""
    pid = int(info.get("pid") or 0)
    mode = instance_texture_mode(info)
    if not pid:
        return "left", "an instance holds the lock but its pid is unknown"
    if mode not in RESTARTABLE_MODES:
        what = "uses texture mode %s" % mode if mode else "may use SetOverlayRaw (its texture mode cannot be told)"
        return "left", ("running instance (pid %d) %s and was not stopped (stopping a raw-mode overlay crashes "
                        "vrcompositor)" % (pid, what))
    kill = kill or os.kill
    alive = alive or process_alive
    try:
        kill(pid, signal.SIGTERM)
    except ProcessLookupError:   # it exited since it was looked up
        return "exited", "pid %d had already exited" % pid
    except PermissionError as e:
        return "left", "cannot stop pid %d (%s)" % (pid, e)
    deadline = clock() + timeout
    while alive(pid):
        if clock() >= deadline:
            return "timeout", "pid %d did not exit within %.0f s after SIGTERM" % (pid, timeout)
        sleep(0.1)
    return "stopped", "stopped pid %d (%s mode)" % (pid, mode)


def restart_instance(info, relaunch, timeout=5.0, kill=None, alive=None, sleep=time.sleep, clock=time.monotonic):
    """Stops a running instance so the new install takes over, then returns relaunch()'s note. A raw-mode
    instance, or one whose mode is unknown, is left running: stopping it can crash the compositor."""
    outcome, text = stop_instance(info, timeout, kill, alive, sleep, clock)
    if outcome == "stopped":
        return "restarted: %s, %s" % (text, relaunch())
    if outcome == "exited":
        return "%s, %s" % (text, relaunch())
    if outcome == "timeout":
        return text + "; not relaunched (see --status)"
    return text + "; restart required: reboot the headset"


def stop_unless_raw(pid, instance):
    """--uninstall while SteamVR runs, or may run: SIGTERM to pid only in a texture mode known to exit
    safely (the rule of restart_instance). Returns (signalled, text)."""
    own = instance is not None and int(instance.get("pid") or 0) == pid
    mode = instance_texture_mode(instance if own else {"pid": pid, "legacy": True})
    if mode in RESTARTABLE_MODES:
        return True, stop_pid(pid)
    return False, ("pid %d (texture mode %s) was left running: stopping it would crash vrcompositor; "
                   "reboot the headset" % (pid, mode or "unknown, maybe raw"))


def steamvr_process_running(proc="/proc"):
    """True while a SteamVR server or compositor process exists, False when none does, None when that
    cannot be told (no readable /proc)."""
    try:
        entries = os.listdir(proc)
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc, entry, "comm"), "rb") as fh:
                comm = fh.read().decode(errors="replace").strip()
        except OSError:
            continue
        if comm.startswith(STEAMVR_PROCESSES):
            return True
    return False


def steamvr_certainly_down(error):
    """Does a failed VR_Init prove that SteamVR is not running (so that no compositor can hold a buffer
    of a running instance)? Only when the runtime found no server (VRInitError_Init_NoServerForBackgroundApp)
    and no vrserver or vrcompositor process exists. Any other failure (no libopenvr_api.so, a pinned
    interface the runtime does not offer, an IPC or HMD error while SteamVR starts or stops) proves
    nothing."""
    if getattr(error, "code", None) != ovr.VRInitError_Init_NoServerForBackgroundApp:
        return False
    return steamvr_process_running() is False


def stop_running(lock_path=None):
    """--stop: stops the running instance and waits until it exited, so that its tracker port is free
    (e.g. to stage the cdc-acm driver); --start brings it back. Only in a texture mode that exits
    safely: an instance that may use raw mode is left running. Returns (exit status, text)."""
    instance = running_instance(lock_path)
    if instance is None:
        return 0, "no instance is running"
    outcome, text = stop_instance(instance)
    if outcome in ("stopped", "exited"):
        return 0, "%s; start it again with its installed options: python3 %s --start" % (text, os.path.abspath(__file__))
    if outcome == "timeout":
        return 1, text + " (see --status)"
    return 1, text + "; to free the tracker port, unplug the tracker instead, or reboot the headset"


def start_detached(run_args=()):
    """Starts the overlay headless in its own session, surviving the caller's logout."""
    with open(os.devnull, "rb") as stdin, open(os.devnull, "ab") as stdout:
        process = subprocess.Popen([sys.executable, os.path.abspath(__file__)] + list(run_args), cwd=HERE,
                                   stdin=stdin, stdout=stdout, stderr=stdout, start_new_session=True, close_fds=True)
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


def manifest_arguments(path=None):
    with open(path or MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)["applications"][0].get("arguments", "")


def write_manifest_arguments(arguments, path=None):
    """Sets the "arguments" SteamVR launches the overlay with (the options given to --install)."""
    path = path or MANIFEST
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if data["applications"][0].get("arguments", "") == arguments:
        return
    data["applications"][0]["arguments"] = arguments
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent="\t")
        fh.write("\n")
    os.replace(tmp, path)


MANAGEMENT_OPTIONS = ("help", "install", "uninstall", "status", "start", "stop", "no_launch")


def run_arguments(parser, args, skip=()):
    """The options of this command line that change how the overlay runs (everything but the
    management flags and skip), in canonical long form, for the manifest and a headless start."""
    argv = []
    for action in parser._actions:
        if not action.option_strings or action.dest in MANAGEMENT_OPTIONS or action.dest in skip:
            continue
        value = getattr(args, action.dest, action.default)
        if value == action.default:
            continue
        flag = max(action.option_strings, key=len)
        if isinstance(action, argparse._StoreTrueAction):
            argv.append(flag)
        elif isinstance(action, argparse._AppendAction):
            for item in value:
                argv += [flag, str(item)]
        else:
            argv += [flag, str(value)]
    return argv


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


def launch_through_steamvr(apps, run_args):
    try:
        apps.launch(APP_KEY)
    except ovr.OpenVRError as e:
        return "SteamVR did not launch it (%s); started headless (pid %d)" % (e, start_detached(run_args))
    time.sleep(2.0)
    return "launched (pid %d)" % apps.process_id(APP_KEY)


def register_live(enable, launch, library, run_args=(), lock_path=None):
    """Same through the running SteamVR, so it takes effect immediately; with --install a running
    instance is restarted with the new files and options. Returns a status text."""
    instance = running_instance(lock_path)
    try:
        session = ovr.Session(library).init(ovr.VRApplication_Background)
    except ovr.OpenVRError as e:
        # Only "no server" with no SteamVR process left proves that no compositor holds a buffer of
        # a running instance; see steamvr_certainly_down.
        down = steamvr_certainly_down(e)
        note = "SteamVR %s (%s); registration takes effect at the next SteamVR start" % (
            "not running" if down else "not reachable", e)
        if enable and launch:
            def headless():
                return "started headless (pid %d), it connects when SteamVR comes up" % start_detached(run_args)
            note += "; " + (restart_instance(instance, headless) if instance else headless())
        elif enable and instance:
            note += "; pid %d keeps running with its old files and options (--no-launch)" % instance["pid"]
        elif not enable and instance and is_overlay_pid(int(instance.get("pid") or 0), instance):
            if down:
                # no compositor holds a buffer of it now, so even a raw-mode instance may be stopped
                note += "; " + stop_pid(int(instance["pid"]))
            else:
                # SteamVR may be running: the same rule as when it answers
                note += "; " + stop_unless_raw(int(instance["pid"]), instance)[1]
        return note
    try:
        apps = session.applications
        if enable:
            apps.add_manifest(MANIFEST)
            apps.set_auto_launch(APP_KEY, True)
            state = "registered with SteamVR, auto-launch %s" % apps.get_auto_launch(APP_KEY)
            if instance is None:
                pid = apps.process_id(APP_KEY)
                if pid and process_alive(pid):  # running without a lock we can see: treat it like 0.1.x
                    instance = {"pid": pid, "legacy": True}
            if launch:
                def relaunch():
                    return launch_through_steamvr(apps, run_args)
                state += ", " + (restart_instance(instance, relaunch) if instance else relaunch())
            elif instance:
                state += ", pid %d keeps running with its old files and options (--no-launch)" % instance["pid"]
            return state
        pid = int((instance or {}).get("pid") or 0) or apps.process_id(APP_KEY)
        if not is_overlay_pid(pid, instance):
            pid = 0   # not an overlay process (a stale or reused pid): nothing to stop
        note = ""
        if pid:
            # The same rule as for --install: with SteamVR running, stopping an instance that may use
            # SetOverlayRaw crashes vrcompositor.
            signalled, text = stop_unless_raw(pid, instance)
            note = (", " if signalled else "; ") + text
        apps.set_auto_launch(APP_KEY, False)
        apps.remove_manifest(MANIFEST)
        return "removed from SteamVR" + note
    except ovr.OpenVRError as e:
        return "SteamVR registration failed: %s" % e
    finally:
        session.shutdown()


def start_installed(library, lock_path=None):
    """--start: starts this copy with the options --install recorded in its manifest, through SteamVR
    when it runs (which launches it with those arguments), headless otherwise; for example after it
    was stopped so that the cdc-acm driver could be unloaded. Nothing happens while an instance runs."""
    instance = running_instance(lock_path)
    if instance is not None:
        return "already running (pid %d); not started again" % int(instance.get("pid") or 0)
    try:
        run_args = shlex.split(manifest_arguments())
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
        return ("cannot read the options --install recorded in %s (%s); not started. Install it again: "
                "bash <your Babble-Bridge copy>/overlay/install-overlay.sh" % (MANIFEST, e))
    try:
        session = ovr.Session(library).init(ovr.VRApplication_Background)
    except ovr.OpenVRError as e:
        return ("SteamVR not running (%s); started headless (pid %d), it connects when SteamVR comes up"
                % (e, start_detached(run_args)))
    try:
        return launch_through_steamvr(session.applications, run_args)
    finally:
        session.shutdown()


def mode_text(mode):
    """A texture mode for --status; one that may be raw carries the warning."""
    if mode in RESTARTABLE_MODES:
        return mode
    return ("%s (may be raw: do not stop it, --stop refuses it; to free the tracker port unplug the tracker, "
            "or reboot the headset)" % (mode or "unknown"))


def status(library, lock_path=None):
    cfg = config_dir()
    lines = ["manifest: %s" % MANIFEST]
    try:
        arguments = manifest_arguments()
        lines.append("manifest arguments: %s" % (arguments or "(none)"))
        lines.append("texture mode: %s" % texture_mode_from_argv(shlex.split(arguments), DEFAULT_TEXTURE_MODE))
    except (OSError, ValueError, KeyError, IndexError) as e:
        lines.append("manifest arguments: unreadable (%s)" % e)
    lines.append("config dir: %s" % cfg)
    info = running_instance(lock_path)
    if info is None:
        lines.append("running instance (lock): none (start it with its installed options: python3 %s --start)"
                     % os.path.abspath(__file__))
    elif info.get("legacy"):
        lines.append("running instance (lock): pid %d, version 0.1.x (%s), texture mode %s"
                     % (info.get("pid", 0), info["lock"], mode_text(instance_texture_mode(info))))
    else:
        heartbeat = info.get("heartbeat")
        age = "%ds ago" % (time.time() - heartbeat) if isinstance(heartbeat, (int, float)) else "?"
        lines.append("running instance (lock): pid %d, version %s, texture mode %s, texture path %s, heartbeat %s"
                     % (info.get("pid", 0), info.get("version", "?"), mode_text(instance_texture_mode(info)),
                        info.get("sink", "?"), age))
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

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_DATEFMT = "%H:%M:%S"


def truncate_log(path, limit=1024 * 1024):
    try:
        if os.path.getsize(path) > limit:
            os.replace(path, path + ".old")
    except OSError:
        pass


def attach_log_file(path):
    """Rotates the log file (over 1 MiB) and logs to it too. Only the instance that holds the lock
    does this: a duplicate launch that exits at once would move the running instance's log away."""
    truncate_log(path)
    handler = logging.FileHandler(path)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
    logging.getLogger().addHandler(handler)
    return handler


def detach_log_file(handler):
    if handler is not None:
        logging.getLogger().removeHandler(handler)
        handler.close()


def build_parser():
    parser = fcam_bridge.build_parser()
    parser.description = "FCAM bridge as a SteamVR overlay."
    parser.add_argument("--install", action="store_true",
                        help="register with SteamVR (recording the other options given here in the manifest), "
                             "enable auto-launch and (re)start")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove the SteamVR registration and stop it (while SteamVR runs, an instance "
                             "that may use raw mode is left running: stopping it would crash the compositor)")
    parser.add_argument("--status", action="store_true", help="print registration state and exit")
    parser.add_argument("--start", action="store_true",
                        help="start it again with the options --install recorded (through SteamVR when it "
                             "runs, headless otherwise); nothing happens while an instance runs")
    parser.add_argument("--stop", action="store_true",
                        help="stop the running instance and wait until it exited (frees the tracker port); an "
                             "instance that may use raw mode is left running (stopping it would crash the "
                             "compositor); --start brings it back")
    parser.add_argument("--no-launch", action="store_true", help="with --install: do not (re)start the overlay now")
    parser.add_argument("--openvr-lib", default=None, help="path to libopenvr_api.so (auto-detected)")
    parser.add_argument("--texture-mode", choices=TEXTURE_MODES, default=DEFAULT_TEXTURE_MODE,
                        help="how the panel reaches SteamVR: 'file' (PNG via SetOverlayFromFile, default), 'gl' "
                             "(persistent GL texture via SetOverlayTexture), 'auto' (gl, else file), 'none' "
                             "(no texture); 'raw' needs --unsafe-raw")
    parser.add_argument("--unsafe-raw", action="store_true",
                        help="debugging only: allow --texture-mode raw (SetOverlayRaw), which crashes the Steam "
                             "Frame compositor when the overlay exits")
    parser.add_argument("--app-type", choices=("overlay", "background"), default="overlay",
                        help="how to identify to SteamVR (VRApplication_Overlay or _Background)")
    parser.add_argument("--log-file", default=DEFAULT_LOG, help="log file (default: next to this script)")
    parser.add_argument("--lock-file", default=None,
                        help="single-instance lock (default: $XDG_RUNTIME_DIR/%s)" % LOCK_NAME)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.texture_mode == "raw" and not args.unsafe_raw:
        parser.error(RAW_REFUSED)

    if args.stop and (args.install or args.uninstall or args.status or args.start):
        parser.error("--stop goes alone (with --lock-file at most)")
    if args.start and (args.install or args.uninstall or args.status):
        parser.error("--start goes alone (with --openvr-lib or --lock-file at most)")
    if args.stop:
        extra = run_arguments(parser, args, skip=("lock_file",))
        if extra:
            parser.error("--stop takes no run options, not %s" % shlex.join(extra))
        code, text = stop_running(args.lock_file)
        print(text)
        return code
    if args.status:
        print(status(args.openvr_lib, args.lock_file))
        return 0
    if args.install or args.uninstall:
        enable = bool(args.install)
        run_args = run_arguments(parser, args) if enable else []
        if enable:
            arguments = shlex.join(run_args)
            write_manifest_arguments(arguments)
            print("texture mode: %s (manifest arguments: %s)" % (args.texture_mode, arguments or "none"))
        appconfig, appcfg = register_files(enable)
        print("%s %s and %s" % ("updated" if enable else "cleaned", appconfig, appcfg))
        print(register_live(enable, launch=not args.no_launch, library=args.openvr_lib, run_args=run_args,
                            lock_path=args.lock_file))
        return 0
    if args.start:
        extra = run_arguments(parser, args, skip=("openvr_lib", "lock_file"))
        if extra:
            parser.error("--start uses the options --install recorded (see --status), not %s" % shlex.join(extra))
        print(start_installed(args.openvr_lib, args.lock_file))
        return 0

    # Standard error only until this is the instance that runs (see attach_log_file).
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=LOG_FORMAT,
                        datefmt=LOG_DATEFMT)
    lock = InstanceLock(args.lock_file or default_lock_path())
    if not acquire_instance_lock(lock, dict(pid=os.getpid(), version=fcam_bridge.__version__,
                                            texture_mode=args.texture_mode, heartbeat=int(time.time()))):
        other = probe_lock(lock.path) or {}
        log.info("another FCAM overlay instance is running (pid %s, %s); exiting", other.get("pid", "?"), lock.path)
        return 0
    legacy = probe_lock(LEGACY_LOCK)
    if legacy is not None:
        log.info("an FCAM overlay 0.1.x instance is running (pid %s, %s); exiting", legacy.get("pid", "?"),
                 LEGACY_LOCK)
        lock.close()
        return 0

    log_file = attach_log_file(args.log_file) if args.log_file else None
    try:
        try:
            app = OverlayApp(args, lock=lock)
        except OSError as e:
            log.error("cannot start the bridge (%s); is UDP port %s already in use?", e, args.listen)
            return 1

        def request_stop(signum, _frame):
            log.info("signal %d, stopping", signum)
            app.stop.set()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        app.run()
    finally:
        lock.close()
        detach_log_file(log_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
