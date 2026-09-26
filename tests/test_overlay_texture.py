"""Unit tests for the overlay's texture paths (GL, file, none, raw), its upload scheduling, error
handling, teardown order and the OpenVR/GL bindings. Nothing here needs SteamVR, EGL or Linux:
sessions, overlays and GL are fakes, and the clock is injected. Standard library only:

    python3 -m unittest discover -s tests -v
"""
import contextlib
import ctypes
import io
import logging
import os
import re
import shutil
import signal
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "overlay"))

import fcam_bridge  # noqa: E402
import fcam_overlay  # noqa: E402
import gl_texture  # noqa: E402
import openvr_min  # noqa: E402

for _name in ("fcam", "gl_texture"):  # keep expected warnings out of the test output
    logging.getLogger(_name).addHandler(logging.NullHandler())

MAIN_HANDLE = 0x1000


# ----------------------------------------------------------------------------- fakes

class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class InstantEvent(threading.Event):
    """A stop event whose wait() advances the fake clock instead of sleeping."""

    def __init__(self, clock):
        super().__init__()
        self.clock = clock

    def wait(self, timeout=None):
        self.clock.advance(timeout or 0.0)
        return self.is_set()


class FakeRuntime:
    def __init__(self, calls, changing=False):
        self.calls = calls
        self.changing = changing
        self.count = 0
        self.stopped = False

    def start(self):
        self.calls.append("runtime.start")

    def stop(self):
        self.calls.append("runtime.stop")
        self.stopped = True

    def snapshot(self):
        self.count += 1
        subscribers = ["10.0.0.%d:5000" % self.count] if self.changing else ["10.0.0.2:5000"]
        return {"state": "streaming", "source": "/dev/ttyACM0", "fps": 60.0, "frames": 100 * self.count,
                "bad": 0, "dropped": 0, "unsent": 0, "send_errors": 0, "subscribers": subscribers,
                "listen_port": 8555, "uptime": 5, "opens": 1}


class FakeOverlay:
    version = "IVROverlay_028"

    def __init__(self, calls, clock):
        self.calls = calls
        self.clock = clock
        self.created = False
        self.visible = False
        self.events = []
        self.fail_codes = []          # codes raised by the next texture calls (0 = success)
        self.gl_reject = 0            # code every SetOverlayTexture fails with (0 = accepted)
        self.find_override = None
        self.find_code = 0            # error code FindOverlay answers with (0 = found)
        self.uploads = []             # (time, call) of every texture hand-over on the main overlay

    def find_overlay(self, key):
        if self.find_override is not None:
            return self.find_override
        return MAIN_HANDLE if self.created else None

    def find_overlay_code(self, key):
        if self.find_code:
            return self.find_code, None
        handle = self.find_overlay(key)
        return (0, handle) if handle else (openvr_min.VROverlayError_UnknownOverlay, None)

    def destroy_overlay(self, handle):
        self.calls.append("destroy")
        return 0

    def create_dashboard_overlay(self, key, name):
        self.created = True
        return MAIN_HANDLE, MAIN_HANDLE + 1

    def set_width_meters(self, handle, width):
        pass

    def set_input_method(self, handle, method):
        pass

    def set_mouse_scale(self, handle, width, height):
        pass

    def _texture_call(self, name, handle):
        assert handle == MAIN_HANDLE
        self.calls.append(name)
        self.uploads.append((self.clock(), name))
        code = self.fail_codes.pop(0) if self.fail_codes else 0
        if code:
            raise openvr_min.OpenVRError("%s failed: fake (%d)" % (name, code), code)

    def set_gl_texture(self, handle, texture_id, color_space=openvr_min.ColorSpace_Gamma):
        self._texture_call("set_gl_texture", handle)
        if self.gl_reject:
            raise openvr_min.OpenVRError("SetOverlayTexture failed: fake (%d)" % self.gl_reject, self.gl_reject)

    def set_from_file(self, handle, path):
        self._texture_call("set_from_file", handle)

    def set_raw(self, handle, pixels, width, height, bytes_per_pixel=4):
        self._texture_call("set_raw", handle)

    def clear_texture(self, handle):
        self.calls.append("clear")
        return 0

    def is_visible(self, handle):
        return self.visible

    def poll_next_overlay_event(self, handle):
        return self.events.pop(0) if self.events else None

    def texture_size(self, handle):
        return fcam_overlay.PANEL_W, fcam_overlay.PANEL_H


class FakeSystem:
    version = "IVRSystem_026"

    def __init__(self):
        self.events = []
        self.acknowledged = False

    def poll_next_event(self):
        return self.events.pop(0) if self.events else None

    def acknowledge_quit_exiting(self):
        self.acknowledged = True

    def runtime_version(self):
        return "2.17.10"


class FakeApplications:
    version = "IVRApplications_008"

    def identify(self, pid, key):
        pass


class FakeSession:
    library_path = "/fake/libopenvr_api.so"

    def __init__(self, calls, overlay):
        self.calls = calls
        self.system = FakeSystem()
        self.overlay = overlay
        self.applications = FakeApplications()

    def shutdown(self):
        self.calls.append("shutdown")


class FakeGl:
    def __init__(self, calls, fail=False):
        self.calls = calls
        self.fail = fail
        self.texture = 7
        self.info = "fake renderer"
        self.closed = None

    def upload(self, buf):
        self.calls.append("gl.upload")
        if self.fail:
            raise gl_texture.GlError("fake GL failure")

    def finish(self):
        self.calls.append("finish")

    def close(self, terminate=False):
        self.calls.append("gl.close")
        self.closed = {"terminate": terminate}


class FakeSink(fcam_overlay.TextureSink):
    mode = "gl"
    label = "fake"

    def __init__(self, clock):
        self.clock = clock
        self.errors = []
        self.times = []

    def submit(self, overlay, handle, panel):
        self.times.append(self.clock())
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        return True


def vr_event(kind, x=0.0, y=0.0, button=0):
    event = openvr_min.VREvent_t()
    event.eventType = kind
    struct.pack_into("<ffI", event.data, 0, x, y, button)
    return event


class AppTestCase(unittest.TestCase):
    """Builds an OverlayApp wired to fakes."""

    def make_app(self, mode="gl", gl_fail=False, changing=False, env=None, sink=None):
        self.calls = []
        self.clock = FakeClock()
        self.gls = []
        self.sessions = []
        self.overlay = FakeOverlay(self.calls, self.clock)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

        def gl_factory():
            gl = FakeGl(self.calls, fail=gl_fail)
            self.gls.append(gl)
            return gl

        def session_factory(app_type):
            self.overlay.created = False
            session = FakeSession(self.calls, self.overlay)
            self.sessions.append(session)
            return session

        args = fcam_overlay.build_parser().parse_args(["--texture-mode", mode])
        self.runtime = FakeRuntime(self.calls, changing=changing)
        app = fcam_overlay.OverlayApp(
            args, runtime=self.runtime, session_factory=session_factory, gl_factory=gl_factory,
            file_factory=lambda label="file": fcam_overlay.FileSink(self.tmp, label=label),
            clock=self.clock, env=dict({"FCAM_DEBUG_NO_THUMB": "1"}, **(env or {})))
        app.ip_provider = lambda: "10.0.0.5"
        if sink is not None:
            app.sink = sink
        self.addCleanup(app.close_sink)
        return app

    def run_loop(self, app, seconds, until=None):
        """OverlayApp.run() on the fake clock for at most seconds (or until until() is true). Returns
        the texture sink that was in use when it stopped (shutdown closes it)."""
        app.stop = InstantEvent(self.clock)
        end = self.clock() + seconds
        real_periodic, real_shutdown = app.periodic, app.shutdown
        final = []

        def periodic(now):
            if now >= end or (until is not None and until()):
                app.stop.set()
            real_periodic(now)

        def shutdown():
            final.append(app.sink)
            real_shutdown()

        app.periodic, app.shutdown = periodic, shutdown
        app.run()
        return final[0]

    def run_for(self, app, seconds, tick=0.01, each=None):
        end = self.clock() + seconds
        while self.clock() < end - 1e-9:
            self.clock.advance(tick)
            if each is not None:
                each()
            app.step(self.clock())

    def texture_calls(self, name):
        return [t for t, call in self.overlay.uploads if call == name]


# ----------------------------------------------------------------------------- sink selection

class ResolveSinkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file_factory = lambda label="file": fcam_overlay.FileSink(tmp.name, label=label)

    def failing(self, error):
        def factory():
            raise error
        return factory

    def test_auto_prefers_gl(self):
        sink = fcam_overlay.resolve_sink("auto", lambda: FakeGl([]), self.file_factory)
        self.assertIsInstance(sink, fcam_overlay.GlSink)
        self.assertEqual("GL", sink.label)

    def test_auto_falls_back_to_file_when_gl_fails(self):
        for error in (OSError("libEGL.so.1: cannot open"), gl_texture.GlError("eglInitialize failed"),
                      RuntimeError("gl_texture.py is not installed")):
            with self.assertLogs("fcam.overlay", "ERROR") as logs:
                sink = fcam_overlay.resolve_sink("auto", self.failing(error), self.file_factory)
            self.assertIsInstance(sink, fcam_overlay.FileSink)
            self.assertEqual("file (GL failed)", sink.label)
            self.assertIn("using SetOverlayFromFile", logs.output[0])

    def test_strict_gl_falls_back_to_nothing(self):
        with self.assertLogs("fcam.overlay", "ERROR"):
            sink = fcam_overlay.resolve_sink("gl", self.failing(gl_texture.GlError("no display")), self.file_factory)
        self.assertIsInstance(sink, fcam_overlay.NoneSink)
        self.assertEqual("none (GL failed)", sink.label)
        self.assertFalse(sink.uploads)

    def test_plain_modes(self):
        self.assertIsInstance(fcam_overlay.resolve_sink("file", None, self.file_factory), fcam_overlay.FileSink)
        self.assertIsInstance(fcam_overlay.resolve_sink("none", None, self.file_factory), fcam_overlay.NoneSink)

    def test_raw_only_when_explicitly_unsafe(self):
        with self.assertRaises(ValueError):
            fcam_overlay.resolve_sink("raw", None, self.file_factory)
        self.assertIsInstance(fcam_overlay.resolve_sink("raw", None, self.file_factory, unsafe_raw=True),
                              fcam_overlay.RawSink)

    def test_no_automatic_path_ever_yields_raw(self):
        factories = [lambda: FakeGl([]), self.failing(OSError("x")), self.failing(gl_texture.GlError("x")),
                     self.failing(RuntimeError("x"))]
        with self.assertLogs("fcam.overlay", "ERROR"):
            for mode in ("auto", "gl", "file", "none"):
                for factory in factories:
                    for unsafe in (False, True):
                        sink = fcam_overlay.resolve_sink(mode, factory, self.file_factory, unsafe_raw=unsafe)
                        self.assertNotIsInstance(sink, fcam_overlay.RawSink, (mode, unsafe))

    def test_missing_gl_texture_module_falls_back(self):
        with mock.patch.object(fcam_overlay, "gl_texture", None), self.assertLogs("fcam.overlay", "ERROR") as logs:
            sink = fcam_overlay.resolve_sink("auto", None, self.file_factory)
        self.assertIsInstance(sink, fcam_overlay.FileSink)
        self.assertIn("gl_texture.py", logs.output[0])


class ArgumentTests(unittest.TestCase):
    def test_default_is_auto_mode(self):
        args = fcam_overlay.build_parser().parse_args([])
        self.assertEqual("auto", args.texture_mode)
        self.assertFalse(args.unsafe_raw)
        self.assertEqual(("auto", "gl", "file", "none", "raw"), fcam_overlay.TEXTURE_MODES)

    def test_raw_without_unsafe_raw_is_a_parser_error(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            fcam_overlay.main(["--texture-mode", "raw"])
        self.assertEqual(2, caught.exception.code)
        self.assertIn("--unsafe-raw", stderr.getvalue())
        self.assertIn("SIGBUS", stderr.getvalue())

    def test_run_arguments_keep_only_run_options(self):
        parser = fcam_overlay.build_parser()
        args = parser.parse_args(["--install", "--no-launch", "--texture-mode", "file", "--target", "10.0.0.2",
                                  "--target", "10.0.0.3:9000", "-v", "--listen", "0.0.0.0:8555"])
        self.assertEqual(["--target", "10.0.0.2", "--target", "10.0.0.3:9000", "--verbose", "--texture-mode", "file"],
                         fcam_overlay.run_arguments(parser, args))
        self.assertEqual([], fcam_overlay.run_arguments(parser, parser.parse_args(["--install"])))
        self.assertEqual([], fcam_overlay.run_arguments(parser, parser.parse_args(["--install", "--texture-mode",
                                                                                   "auto"])))
        self.assertEqual([], fcam_overlay.run_arguments(parser, parser.parse_args(["--start"])))

    def test_start_takes_no_run_options(self):
        for argv in (["--start", "--texture-mode", "gl"], ["--start", "--install"], ["--start", "--status"]):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                fcam_overlay.main(argv)
            self.assertEqual(2, caught.exception.code, argv)
            self.assertIn("--start", stderr.getvalue())

    def test_stop_goes_alone_and_is_never_recorded(self):
        for argv in (["--stop", "--texture-mode", "gl"], ["--stop", "--start"], ["--stop", "--install"],
                     ["--stop", "--uninstall"], ["--stop", "--status"]):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                fcam_overlay.main(argv)
            self.assertEqual(2, caught.exception.code, argv)
            self.assertIn("--stop", stderr.getvalue())
        parser = fcam_overlay.build_parser()
        self.assertEqual([], fcam_overlay.run_arguments(parser, parser.parse_args(["--stop"])))
        self.assertIn("stop", fcam_overlay.MANAGEMENT_OPTIONS)

    def test_install_records_options_in_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = os.path.join(tmp, "fcam.vrmanifest")
            shutil.copy(os.path.join(ROOT, "overlay", "fcam.vrmanifest"), manifest)
            with open(manifest, "rb") as fh:
                original = fh.read()
            recorded = []

            def fake_register_live(enable, launch, library, run_args=(), lock_path=None):
                recorded.append((enable, launch, list(run_args)))
                return "registered (fake)"

            with mock.patch.object(fcam_overlay, "MANIFEST", manifest), \
                    mock.patch.object(fcam_overlay, "config_dir", lambda: tmp), \
                    mock.patch.object(fcam_overlay, "register_live", fake_register_live), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(0, fcam_overlay.main(["--install", "--texture-mode", "file"]))
                self.assertEqual("--texture-mode file", fcam_overlay.manifest_arguments(manifest))
                self.assertEqual((True, True, ["--texture-mode", "file"]), recorded[-1])
                self.assertIn("texture mode: file", out.getvalue())
                # installing again without options goes back to the defaults
                self.assertEqual(0, fcam_overlay.main(["--install", "--no-launch"]))
                self.assertEqual("", fcam_overlay.manifest_arguments(manifest))
                self.assertEqual((True, False, []), recorded[-1])
                self.assertIn("texture mode: auto (manifest arguments: none)", out.getvalue())
            with open(manifest, "rb") as fh:
                self.assertEqual(original, fh.read())
            self.assertFalse(os.path.exists(manifest + ".tmp"))


# ----------------------------------------------------------------------------- file sink

def png_chunks(data):
    """Parses a PNG and checks every chunk CRC; returns [(tag, payload)]."""
    assert data.startswith(fcam_overlay.PNG_SIGNATURE)
    pos, chunks = 8, []
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
        assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF, tag
        chunks.append((tag, payload))
        pos += 12 + length
    return chunks


class FileSinkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.paths = []
        self.overlay = mock.Mock()
        self.overlay.set_from_file.side_effect = lambda handle, path: self.paths.append(path)

    def panel(self, rgba):
        panel = fcam_overlay.Panel(8, 4)
        panel.fill(rgba)
        return panel

    def test_uploads_only_changed_content_under_alternating_names(self):
        sink = fcam_overlay.FileSink(self.dir)
        red, blue, green = self.panel((255, 0, 0, 255)), self.panel((0, 0, 255, 255)), self.panel((0, 255, 0, 255))
        self.assertTrue(sink.submit(self.overlay, 1, red))
        self.assertFalse(sink.submit(self.overlay, 1, red))  # unchanged: no reload
        self.assertTrue(sink.submit(self.overlay, 1, blue))
        self.assertTrue(sink.submit(self.overlay, 1, green))
        pid = os.getpid()
        a = os.path.join(self.dir, "fcam-panel-%d-a.png" % pid)
        b = os.path.join(self.dir, "fcam-panel-%d-b.png" % pid)
        self.assertEqual([a, b, a], self.paths)
        self.assertEqual(sorted(os.path.basename(p) for p in (a, b)), sorted(os.listdir(self.dir)))  # no .tmp
        with open(a, "rb") as fh:
            chunks = png_chunks(fh.read())
        self.assertEqual([b"IHDR", b"IDAT", b"IEND"], [tag for tag, _ in chunks])
        self.assertEqual((8, 4, 8, 6), struct.unpack(">IIBB", chunks[0][1][:10]))
        raw = zlib.decompress(chunks[1][1])
        self.assertEqual(bytes((0, 255, 0, 255)), raw[1:5])  # green was written last into -a
        sink.connected()  # a new overlay has no texture: the same content is sent again
        self.assertTrue(sink.submit(self.overlay, 1, green))
        sink.close()
        self.assertEqual([], os.listdir(self.dir))

    def test_failed_hand_over_is_retried_with_the_same_name(self):
        sink = fcam_overlay.FileSink(self.dir)
        self.overlay.set_from_file.side_effect = openvr_min.OpenVRError("SetOverlayFromFile failed", 23)
        panel = self.panel((1, 2, 3, 255))
        with self.assertRaises(openvr_min.OpenVRError):
            sink.submit(self.overlay, 1, panel)
        self.overlay.set_from_file.side_effect = lambda handle, path: self.paths.append(path)
        self.assertTrue(sink.submit(self.overlay, 1, panel))
        self.assertTrue(self.paths[0].endswith("-a.png"))
        self.assertFalse([name for name in os.listdir(self.dir) if name.endswith(".tmp")])
        sink.close()

    def test_write_error_leaves_no_temporary_file(self):
        sink = fcam_overlay.FileSink(self.dir)
        with mock.patch.object(fcam_overlay.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sink.submit(self.overlay, 1, self.panel((9, 9, 9, 255)))
        self.assertEqual([], os.listdir(self.dir))

    def test_directory_prefers_the_runtime_dir(self):
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.dir}):
            self.assertEqual(self.dir, fcam_overlay.panel_file_dir())
            self.assertTrue(fcam_overlay.FileSink().paths[0].startswith(self.dir))


class FileModeSchedulingTests(AppTestCase):
    def test_min_interval_is_respected(self):
        app = self.make_app("file", changing=True)
        self.assertTrue(app.connect_vr())
        self.overlay.visible = True
        self.run_for(app, 10.0, tick=0.05, each=lambda: setattr(app, "dirty", True))
        times = self.texture_calls("set_from_file")
        self.assertGreaterEqual(len(times), 4)
        self.assertLessEqual(len(times), 6)
        for earlier, later in zip(times, times[1:]):
            self.assertGreaterEqual(later - earlier, fcam_overlay.FILE_MIN_INTERVAL - 1e-6)

    def test_unchanged_panel_is_not_reloaded(self):
        app = self.make_app("file", changing=False)
        self.assertTrue(app.connect_vr())
        self.overlay.visible = True
        self.run_for(app, 10.0, tick=0.05)
        self.assertEqual(1, len(self.texture_calls("set_from_file")))

    def test_waiting_for_a_client_does_not_reload(self):
        # The tracker streams at 60 fps, nobody is subscribed yet: "unsent" grows with every frame
        # while the user reads the address off the tab.
        app = self.make_app("file")
        start = self.clock()

        def snapshot():
            elapsed = self.clock() - start
            return {"state": "streaming", "source": "/dev/ttyACM0", "fps": 0.0, "frames": 0, "bad": 0,
                    "dropped": 0, "unsent": int(elapsed * 60), "send_errors": 0, "subscribers": [], "targets": [],
                    "listen_port": 8555, "uptime": 5 + int(elapsed), "opens": 1}

        self.runtime.snapshot = snapshot
        self.assertTrue(app.connect_vr())
        self.overlay.visible = True
        self.run_for(app, 300.0, tick=0.05)
        self.assertLessEqual(len(self.texture_calls("set_from_file")), 6)   # the uptime minutes


# ----------------------------------------------------------------------------- scheduling and errors

class SchedulingTests(AppTestCase):
    def test_hidden_tab_gets_no_uploads_after_the_first(self):
        app = self.make_app("gl")
        self.assertTrue(app.connect_vr())
        app.step(self.clock())
        self.assertEqual(1, len(self.texture_calls("set_gl_texture")))  # initial upload on connect
        self.run_for(app, 5.0)
        self.assertEqual(1, len(self.texture_calls("set_gl_texture")))

    def test_visible_tab_is_refreshed_at_4_hz(self):
        app = self.make_app("gl")
        self.assertTrue(app.connect_vr())
        app.step(self.clock())
        self.overlay.visible = True  # noticed by the next is_visible poll, at most 0.25 s later
        self.run_for(app, 2.0)
        times = self.texture_calls("set_gl_texture")[1:]
        self.assertGreaterEqual(len(times), 7)
        self.assertLessEqual(len(times), 8)
        for earlier, later in zip(times, times[1:]):
            self.assertAlmostEqual(fcam_overlay.REDRAW_SECONDS, later - earlier, delta=0.011)

    def test_dirty_redraws_are_limited_to_25_hz(self):
        app = self.make_app("gl")
        self.assertTrue(app.connect_vr())
        self.overlay.visible = True
        self.run_for(app, 1.0, tick=0.005, each=lambda: setattr(app, "dirty", True))
        times = self.texture_calls("set_gl_texture")
        self.assertLessEqual(len(times), 27)
        self.assertGreaterEqual(len(times), 20)
        for earlier, later in zip(times, times[1:]):
            self.assertGreaterEqual(later - earlier, fcam_overlay.DIRTY_MIN_SECONDS - 1e-6)

    def test_failed_upload_waits_5_s_despite_dirty(self):
        app = self.make_app("gl")
        self.assertTrue(app.connect_vr())
        self.overlay.visible = True
        self.overlay.fail_codes = [99]
        app.step(self.clock())
        self.run_for(app, 6.0, tick=0.01, each=lambda: setattr(app, "dirty", True))
        times = self.texture_calls("set_gl_texture")
        self.assertGreaterEqual(times[1] - times[0], fcam_overlay.RETRY_SECONDS - 1e-6)

    def test_forced_uploads_run_while_hidden_and_mark_frames(self):
        app = self.make_app("gl", env={"FCAM_DEBUG_FORCE_UPLOAD_HZ": "25"})
        self.assertTrue(app.connect_vr())
        self.run_for(app, 1.0, tick=0.005)
        self.assertGreaterEqual(len(self.texture_calls("set_gl_texture")), 24)
        self.assertGreaterEqual(app.debug_frame, 24)

    def test_input_only_marks_the_panel_dirty(self):
        app = self.make_app("gl")
        self.assertTrue(app.connect_vr())
        app.step(self.clock())
        before = len(self.calls)
        app.dirty = False
        self.overlay.events = [vr_event(openvr_min.VREvent_MouseMove, 320, fcam_overlay.PANEL_H - 10)]
        self.assertTrue(app.handle_events())
        self.assertTrue(app.hot_button)
        self.assertTrue(app.dirty)
        self.assertEqual(before, len(self.calls))  # no upload from the event handler
        self.overlay.events = [vr_event(openvr_min.VREvent_FocusLeave)]
        app.handle_events()
        self.assertFalse(app.hot_button)
        app.dirty = False
        self.overlay.events = [vr_event(openvr_min.VREvent_OverlayShown)]
        app.handle_events()
        self.assertTrue(app.visible)
        self.assertTrue(app.dirty)
        self.overlay.events = [vr_event(openvr_min.VREvent_OverlayHidden)]
        app.handle_events()
        self.assertFalse(app.visible)
        with mock.patch.object(app, "restart_bridge") as restart:
            self.overlay.events = [vr_event(openvr_min.VREvent_MouseButtonDown, 320, 10, 1)]
            app.handle_events()
        restart.assert_called_once_with()
        self.assertEqual(before, len(self.calls))

    def test_quit_event_stops(self):
        app = self.make_app("gl")
        self.assertTrue(app.connect_vr())
        session = self.sessions[-1]
        session.system.events = [vr_event(openvr_min.VREvent_ProcessQuit), vr_event(openvr_min.VREvent_Quit)]
        self.assertFalse(app.step(self.clock()))
        self.assertTrue(session.system.acknowledged)

    def test_restart_bridge_survives_errors(self):
        app = self.make_app("gl")
        app.runtime_factory = mock.Mock(side_effect=RuntimeError("port in use"))
        with mock.patch.object(fcam_overlay.time, "sleep"), self.assertLogs("fcam.overlay", "ERROR"):
            app.restart_bridge()
        self.assertTrue(self.runtime.stopped)


class ErrorClassificationTests(AppTestCase):
    def test_classify(self):
        classify = fcam_overlay.classify_upload_error
        self.assertEqual("standby", classify(openvr_min.OpenVRError("x", 23)))
        self.assertEqual("standby", classify(openvr_min.OpenVRError("x", 34)))
        self.assertEqual("reconnect", classify(openvr_min.OpenVRError("x", 10)))
        self.assertEqual("reconnect", classify(openvr_min.OpenVRError("x", 11)))
        self.assertEqual("other", classify(openvr_min.OpenVRError("x", 24)))
        self.assertEqual("gl", classify(openvr_min.OpenVRError("x", 24), gl_sink=True))   # InvalidTexture
        self.assertEqual("standby", classify(openvr_min.OpenVRError("x", 23), gl_sink=True))
        self.assertEqual("other", classify(openvr_min.OpenVRError("x", 20), gl_sink=True))
        self.assertEqual("other", classify(openvr_min.OpenVRError("x")))
        self.assertEqual("other", classify(OSError("disk full")))
        self.assertEqual("gl", classify(gl_texture.GlError("x")))

    def connected_app(self):
        sink = FakeSink(None)
        app = self.make_app("gl", sink=sink)
        sink.clock = self.clock
        self.assertTrue(app.connect_vr())
        return app, sink

    def test_standby_backs_off_5_s_without_reconnecting(self):
        for code in (23, 34):
            app, sink = self.connected_app()
            sink.errors = [openvr_min.OpenVRError("SetOverlayTexture failed", code)]
            with self.assertLogs("fcam.overlay", "WARNING"):
                app.step(self.clock())
            self.clock.advance(4.9)
            app.step(self.clock())
            self.assertEqual(1, len(sink.times))
            self.clock.advance(0.1)
            with self.assertLogs("fcam.overlay", "INFO") as logs:
                app.step(self.clock())
            self.assertEqual(2, len(sink.times))
            self.assertTrue(any("uploads work again" in line for line in logs.output))

    def test_unknown_overlay_or_invalid_handle_reconnects(self):
        for code in (10, 11):
            app, sink = self.connected_app()
            sink.errors = [openvr_min.OpenVRError("SetOverlayTexture failed", code)]
            with self.assertRaises(fcam_overlay.ReconnectNeeded):
                app.step(self.clock())

    def test_other_errors_reconnect_after_30_s(self):
        app, sink = self.connected_app()
        sink.errors = [openvr_min.OpenVRError("SetOverlayTexture failed", 24)] * 20
        start = self.clock()
        with self.assertLogs("fcam.overlay", "WARNING"):
            with self.assertRaises(fcam_overlay.ReconnectNeeded):
                for _ in range(20):
                    app.step(self.clock())
                    self.clock.advance(fcam_overlay.RETRY_SECONDS)
        self.assertAlmostEqual(fcam_overlay.FAIL_RECONNECT_SECONDS, sink.times[-1] - start)

    def test_liveness_probe_reconnects_when_the_overlay_changed(self):
        app, sink = self.connected_app()
        app.step(self.clock())
        self.overlay.find_override = 0x2222
        self.clock.advance(fcam_overlay.LIVENESS_SECONDS)
        with self.assertRaises(fcam_overlay.ReconnectNeeded):
            app.step(self.clock())

    def test_liveness_probe_reconnects_when_the_overlay_is_unknown(self):
        for code in (10, 11):   # UnknownOverlay, InvalidHandle
            app, sink = self.connected_app()
            app.step(self.clock())
            self.overlay.find_code = code
            self.clock.advance(fcam_overlay.LIVENESS_SECONDS)
            with self.assertRaises(fcam_overlay.ReconnectNeeded):
                app.step(self.clock())

    def test_liveness_probe_does_not_take_standby_for_a_lost_overlay(self):
        # A suspended compositor (or one IPC timeout) answers FindOverlay with RequestFailed/TimedOut:
        # that must not tear down a working overlay every 5 s. Other codes do not reconnect either.
        for code in (23, 34, 20):
            app, sink = self.connected_app()
            app.step(self.clock())
            self.overlay.find_code = code
            for _ in range(4):
                self.clock.advance(fcam_overlay.LIVENESS_SECONDS)
                app.step(self.clock())
            self.assertIs(self.sessions[-1], app.session, code)
            self.assertEqual([], [c for c in self.calls if c in ("destroy", "shutdown")], code)
            # awake again with the overlay still there: nothing happens either
            self.overlay.find_code = 0
            self.clock.advance(fcam_overlay.LIVENESS_SECONDS)
            app.step(self.clock())
            self.assertIs(self.sessions[-1], app.session)

    def test_an_unusable_runtime_is_logged_as_an_error(self):
        app = self.make_app("file")

        def unusable(app_type):
            raise openvr_min.OpenVRError("runtime supports none of IVRSystem_026")

        def not_up(app_type):
            raise openvr_min.OpenVRError("VR_Init failed (121) VRInitError_Init_NoServerForBackgroundApp", 121)

        app.session_factory = unusable
        with self.assertLogs("fcam.overlay", "ERROR"):
            self.assertFalse(app.connect_vr())
        app.session_factory = not_up
        self.clock.advance(fcam_overlay.VR_RETRY_SECONDS)
        with self.assertLogs("fcam.overlay", "INFO") as logs:
            self.assertFalse(app.connect_vr())
        self.assertEqual(["INFO"], [line.split(":", 1)[0] for line in logs.output])

    def test_debug_reconnect_interval(self):
        app = self.make_app("gl", env={"FCAM_DEBUG_RECONNECT_EVERY": "10"})
        self.assertTrue(app.connect_vr())
        self.clock.advance(9.9)
        app.step(self.clock())
        self.clock.advance(0.1)
        with self.assertRaises(fcam_overlay.ReconnectNeeded):
            app.step(self.clock())


class GlRejectedTests(AppTestCase):
    """SteamVR itself refusing the GL texture must end in the same fallback as a GL error."""

    def test_invalid_texture_in_auto_ends_in_file_mode(self):
        app = self.make_app("auto")
        self.overlay.fail_codes = [openvr_min.VROverlayError_InvalidTexture] * (2 * fcam_overlay.GL_STREAK)
        with self.assertLogs("fcam.overlay", "WARNING") as logs:
            sink = self.run_loop(app, 120.0, until=lambda: bool(self.texture_calls("set_from_file")))
        self.assertIsInstance(sink, fcam_overlay.FileSink)
        self.assertEqual("file (GL failed)", sink.label)
        self.assertEqual(1, len(self.texture_calls("set_from_file")))
        self.assertEqual(2, len(self.gls))   # rebuilt once, then given up
        self.assertTrue(any("using SetOverlayFromFile" in line for line in logs.output))

    def test_invalid_texture_in_strict_gl_ends_blank(self):
        app = self.make_app("gl")
        self.overlay.gl_reject = openvr_min.VROverlayError_InvalidTexture
        start = self.clock()
        with self.assertLogs("fcam.overlay", "WARNING"):
            sink = self.run_loop(app, 600.0)
        self.assertIsInstance(sink, fcam_overlay.NoneSink)
        self.assertEqual("none (GL failed)", sink.label)
        # settled within a minute: no reconnect loop for the rest of the 10 minutes
        self.assertLess(max(self.texture_calls("set_gl_texture")) - start, 60.0)
        self.assertLessEqual(len(self.sessions), 3)

    def test_other_error_that_outlasts_a_reconnect_falls_back_while_gl_never_worked(self):
        app = self.make_app("auto")
        self.overlay.gl_reject = 20   # InvalidParameter: not a known GL or standby code
        with self.assertLogs("fcam.overlay", "WARNING"):
            sink = self.run_loop(app, 300.0, until=lambda: bool(self.texture_calls("set_from_file")))
        self.assertIsInstance(sink, fcam_overlay.FileSink)
        self.assertEqual("file (GL failed)", sink.label)
        self.assertLessEqual(len(self.sessions), 4)

    def test_other_errors_after_gl_worked_only_reconnect(self):
        app = self.make_app("auto")
        self.assertTrue(app.connect_vr())
        app.step(self.clock())   # SteamVR took the texture once
        self.overlay.visible = True
        self.overlay.gl_reject = 20
        with self.assertLogs("fcam.overlay", "WARNING"):
            sink = self.run_loop(app, 200.0)
        self.assertIsInstance(sink, fcam_overlay.GlSink)
        self.assertEqual(1, len(self.gls))
        self.assertGreaterEqual(len(self.sessions), 3)   # reconnected every 30 s instead

    def test_standby_never_triggers_the_file_fallback(self):
        app = self.make_app("auto")
        self.overlay.gl_reject = openvr_min.VROverlayError_RequestFailed
        with self.assertLogs("fcam.overlay", "WARNING"):
            sink = self.run_loop(app, 300.0)
        self.assertIsInstance(sink, fcam_overlay.GlSink)
        self.assertEqual(1, len(self.sessions))


class GlStreakTests(AppTestCase):
    def fail_three_times(self, app):
        for _ in range(fcam_overlay.GL_STREAK):
            self.assertIsNotNone(app.session)
            app.step(self.clock())
            self.clock.advance(fcam_overlay.RETRY_SECONDS)

    def check_streaks(self, mode, fallback_type, label):
        app = self.make_app(mode, gl_fail=True)
        with self.assertLogs("fcam.overlay", "WARNING") as logs:
            self.assertTrue(app.connect_vr())
            self.assertIsInstance(app.sink, fcam_overlay.GlSink)
            self.fail_three_times(app)
            # first streak: VR teardown with the context current, then the context is closed and rebuilt
            self.assertIsNone(app.session)
            self.assertEqual(2, len(self.gls))
            self.assertEqual({"terminate": False}, self.gls[0].closed)
            self.assertIsNone(self.gls[1].closed)
            teardown = [c for c in self.calls if c in ("finish", "clear", "destroy", "shutdown", "gl.close")]
            self.assertEqual(["finish", "clear", "destroy", "shutdown", "gl.close"], teardown)
            self.assertIs(self.gls[1], app.sink.gl)
            # second streak within 10 minutes: GL is given up
            self.assertTrue(app.connect_vr())
            self.fail_three_times(app)
            self.assertEqual(2, len(self.gls))
            self.assertIsNotNone(self.gls[1].closed)
            self.assertIsInstance(app.sink, fallback_type)
            self.assertEqual(label, app.sink.label)
        self.assertTrue(any("rebuilding the GL context" in line for line in logs.output))
        self.assertTrue(any("GL texture path unavailable" in line for line in logs.output))
        return app

    def test_auto_switches_to_file_after_a_second_streak(self):
        app = self.check_streaks("auto", fcam_overlay.FileSink, "file (GL failed)")
        self.clock.advance(fcam_overlay.VR_RETRY_SECONDS)
        self.assertTrue(app.connect_vr())
        app.step(self.clock())
        self.assertEqual(1, len(self.texture_calls("set_from_file")))
        self.assertEqual(2, len(self.gls))

    def test_strict_gl_switches_to_none_after_a_second_streak(self):
        app = self.check_streaks("gl", fcam_overlay.NoneSink, "none (GL failed)")
        self.clock.advance(fcam_overlay.VR_RETRY_SECONDS)
        self.assertTrue(app.connect_vr())
        self.assertIsNone(app.next_upload_time())

    def test_streaks_far_apart_rebuild_again(self):
        app = self.make_app("auto", gl_fail=True)
        with self.assertLogs("fcam.overlay", "WARNING"):
            self.assertTrue(app.connect_vr())
            self.fail_three_times(app)
            self.clock.advance(fcam_overlay.GL_STREAK_WINDOW)
            self.assertTrue(app.connect_vr())
            self.fail_three_times(app)
        self.assertEqual(3, len(self.gls))
        self.assertIsInstance(app.sink, fcam_overlay.GlSink)

    def test_success_resets_the_streak(self):
        app = self.make_app("gl", gl_fail=True)
        with self.assertLogs("fcam.overlay", "WARNING"):
            self.assertTrue(app.connect_vr())
            app.step(self.clock())
            self.clock.advance(fcam_overlay.RETRY_SECONDS)
            app.step(self.clock())
            self.assertEqual(2, app.gl_streak)
            self.gls[0].fail = False
            self.clock.advance(fcam_overlay.RETRY_SECONDS)
            app.step(self.clock())
        self.assertEqual(0, app.gl_streak)
        self.assertEqual(1, len(self.gls))


class TeardownTests(AppTestCase):
    def test_exit_order_and_reconnect_keeps_the_context(self):
        app = self.make_app("gl")
        self.assertTrue(app.connect_vr())
        app.step(self.clock())
        del self.calls[:]
        app.disconnect_vr()  # a reconnect: the GL context survives
        self.assertEqual(["finish", "clear", "destroy", "shutdown"], self.calls)
        self.clock.advance(fcam_overlay.VR_RETRY_SECONDS)
        self.assertTrue(app.connect_vr())
        self.assertEqual(1, len(self.gls))
        app.step(self.clock())
        del self.calls[:]
        app.shutdown()  # process exit
        self.assertEqual(["finish", "clear", "destroy", "shutdown", "gl.close", "runtime.stop"], self.calls)
        self.assertEqual({"terminate": False}, self.gls[0].closed)
        self.assertIsNone(app.session)
        self.assertIsNone(app.sink)

    def test_exit_mode_variants(self):
        for mode, expected in (("clear", ["clear", "destroy", "shutdown"]), ("destroy", ["destroy", "shutdown"]),
                               ("nodestroy", ["shutdown"]), ("clear-nodestroy", ["clear", "shutdown"])):
            app = self.make_app("file", env={"FCAM_DEBUG_EXIT_MODE": mode})
            self.assertTrue(app.connect_vr())
            del self.calls[:]
            app.disconnect_vr()
            self.assertEqual(expected, self.calls, mode)

    def test_run_survives_unexpected_errors_and_tears_down(self):
        app = self.make_app("gl")
        app.stop = InstantEvent(self.clock)
        real_step = app.step
        state = {"steps": 0}

        def step(now):
            state["steps"] += 1
            if state["steps"] == 1:
                raise RuntimeError("bug in the overlay")
            if state["steps"] >= 3:
                app.stop.set()
            return real_step(now)

        app.step = step
        with self.assertLogs("fcam.overlay", "INFO") as logs:
            app.run()
        self.assertEqual(3, state["steps"])
        self.assertTrue(any("overlay error" in line and "ERROR" in line for line in logs.output))
        self.assertTrue(self.runtime.stopped)
        self.assertEqual(["finish", "clear", "destroy", "shutdown", "gl.close", "runtime.stop"],
                         [c for c in self.calls if c in ("finish", "clear", "destroy", "shutdown", "gl.close",
                                                         "runtime.stop")][-6:])


# ----------------------------------------------------------------------------- panel

class PanelTests(unittest.TestCase):
    SNAPSHOT = {"state": "streaming", "source": "/dev/ttyACM0", "fps": 59.7, "frames": 1234, "bad": 0,
                "dropped": 0, "unsent": 0, "send_errors": 0, "subscribers": ["10.0.0.2:5000"],
                "listen_port": 8555, "uptime": 65, "opens": 1}

    def draw(self, snapshot, **kwargs):
        panel = fcam_overlay.Panel(fcam_overlay.PANEL_W, fcam_overlay.PANEL_H)
        fcam_overlay.draw_panel(panel, snapshot, False, "2.17.10", "GL", "10.0.0.5", **kwargs)
        return bytes(panel.buf)

    def test_footer_sits_inside_the_card_below_the_rows(self):
        last_row_end = 130 + 4 * (fcam_overlay.CELL_H + 8) + fcam_overlay.CELL_H
        self.assertLessEqual(last_row_end, fcam_overlay.FOOTER_Y)
        self.assertLessEqual(fcam_overlay.FOOTER_Y + fcam_overlay.CELL_H, fcam_overlay.CARD[3])
        self.assertLessEqual(fcam_overlay.VALUE_X + fcam_overlay.VALUE_CHARS * fcam_overlay.CELL_W,
                             fcam_overlay.CARD[2])

    def test_coarse_numbers_hide_per_frame_changes(self):
        later = dict(self.SNAPSHOT, fps=60.2, frames=1300, uptime=100)
        coarse = fcam_overlay.CoarseNumbers()
        self.assertEqual(self.draw(self.SNAPSHOT, coarse=coarse), self.draw(later, coarse=coarse))
        self.assertNotEqual(self.draw(self.SNAPSHOT), self.draw(later))

    def test_coarse_uptime_does_not_tick(self):
        # Every changed panel is a file reload (a blink): the uptime must not change it once a minute.
        wall = FakeClock(1_700_000_000.0)
        coarse = fcam_overlay.CoarseNumbers(wall=wall)
        panels = set()
        for uptime in range(50, 7300, 10):   # two hours, across minute and hour boundaries
            wall.now = 1_700_000_000.0 + uptime + 0.7 * (uptime % 3)   # wall clock and uptime jitter
            panels.add(zlib.crc32(self.draw(dict(self.SNAPSHOT, uptime=uptime), coarse=coarse)))
        self.assertEqual(1, len(panels))
        started = time.strftime("%H:%M", time.localtime(1_700_000_000.0))
        self.assertEqual("since " + started, coarse.since(7300))
        # a bridge restart (the uptime goes back) shows the new start time
        wall.now += 600.0
        self.assertEqual("since " + time.strftime("%H:%M", time.localtime(wall.now - 3)), coarse.since(3))

    def distinct_panels(self, snapshots):
        coarse = fcam_overlay.CoarseNumbers()
        return len({zlib.crc32(self.draw(snapshot, coarse=coarse)) for snapshot in snapshots})

    def test_coarse_panel_is_stable_while_no_client_is_subscribed(self):
        # 60 s at 60 fps, drawn every 0.25 s: unsent counts up with every frame, nothing is sent.
        snapshots = [dict(self.SNAPSHOT, fps=0.0, frames=0, subscribers=[], unsent=15 * i, uptime=5 + i // 4)
                     for i in range(240)]
        self.assertEqual(1, self.distinct_panels(snapshots))

    def test_coarse_frame_rate_does_not_flip_around_a_rounding_boundary(self):
        # 120 s streaming at 56.5-58.5 fps (57.5 is where plain rounding flips between 55 and 60).
        snapshots = [dict(self.SNAPSHOT, fps=57.5 + (1.0 if i % 2 else -1.0) * (i % 7) / 6.0, frames=15 * i,
                          uptime=5 + i // 4) for i in range(480)]
        self.assertEqual(1, self.distinct_panels(snapshots))

    def test_coarse_frame_rate_follows_real_changes(self):
        coarse = fcam_overlay.CoarseNumbers()
        shown = [coarse.fps(fps) for fps in (0.0, 1.0, 29.0, 31.0, 34.9, 35.1, 58.4, 60.0, 56.0, 30.0)]
        self.assertEqual(["0 fps", "0 fps", "~30 fps", "~30 fps", "~30 fps", "~35 fps", "~60 fps", "~60 fps",
                          "~60 fps", "~30 fps"], shown)

    def test_debug_marker_moves(self):
        self.assertNotEqual(self.draw(self.SNAPSHOT, debug_frame=1), self.draw(self.SNAPSHOT, debug_frame=2))

    def test_text_cells_carry_their_background(self):
        panel = fcam_overlay.Panel(64, 32)
        panel.fill(fcam_overlay.BG)
        panel.text(0, 0, " ", fcam_overlay.TEXT, bg=fcam_overlay.PANEL)
        self.assertEqual(bytes(fcam_overlay.PANEL), bytes(panel.buf[0:4]))
        self.assertEqual(bytes(fcam_overlay.BG), bytes(panel.buf[fcam_overlay.CELL_W * 4:fcam_overlay.CELL_W * 4 + 4]))
        panel.text(60, 0, "x")  # does not fit: nothing drawn, no wrap into the next row
        self.assertEqual(bytes(fcam_overlay.BG), bytes(panel.buf[60 * 4:60 * 4 + 4]))

    def test_magnitude(self):
        cases = {0: "0", 7: "7", 10: "10+", 99: "10+", 123: "100+", 999: "100+", 1234: "1k+", 12345: "10k+",
                 123456: "100k+", 1234567: "1M+", 2 * 10 ** 9: "1G+"}
        for value, text in cases.items():
            self.assertEqual(text, fcam_overlay.magnitude(value), value)


# ----------------------------------------------------------------------------- gl_texture

class FakeFn:
    def __init__(self, name, impl, log):
        self.name, self.impl, self.log = name, impl, log
        self.restype = self.argtypes = None

    def __call__(self, *args):
        self.log.append((self.name, args))
        return self.impl(*args)


class FakeLib:
    def __init__(self, log, impls):
        for name, impl in impls.items():
            setattr(self, name, FakeFn(name, impl, log))


class FakeEgl:
    """Just enough EGL and GLES behaviour for GlTexture, with switches for the failure paths."""

    def __init__(self, versions=(3, 2), choose_ok=True, make_current_ok=True, tex_storage=True):
        self.log = []
        self.current = None
        self.gl_errors = []
        self.versions = versions
        self.make_current_ok = make_current_ok

        def choose(display, attribs, config, size, count):
            if not choose_ok:
                return 0
            config._obj.value = 0x2000
            count._obj.value = 1
            return 1

        def create_context(display, config, share, attribs):
            return 0x3000 + attribs[1] if attribs[1] in self.versions else None

        def make_current(display, draw, read, context):
            if context and not self.make_current_ok:
                return 0
            self.current = context
            return 1

        def gen_textures(count, ref):
            ref._obj.value = 5

        self.egl = FakeLib(self.log, {
            "eglGetProcAddress": lambda name: None, "eglGetError": lambda: 0x3009,
            "eglQueryString": lambda display, name: b"", "eglGetDisplay": lambda native: 0x1000,
            "eglInitialize": lambda display, major, minor: 1, "eglBindAPI": lambda api: 1,
            "eglChooseConfig": choose, "eglCreateContext": create_context, "eglMakeCurrent": make_current,
            "eglGetCurrentContext": lambda: self.current, "eglDestroyContext": lambda display, context: 1,
            "eglTerminate": lambda display: 1,
        })
        gl = {
            "glGetString": lambda name: {gl_texture.GL_RENDERER: b"FakeRenderer"}.get(name, b"OpenGL ES 3.2"),
            "glGetError": lambda: self.gl_errors.pop(0) if self.gl_errors else 0,
            "glGenTextures": gen_textures, "glDeleteTextures": lambda count, ref: None,
            "glBindTexture": lambda target, texture: None, "glBindBuffer": lambda target, buffer: None,
            "glTexParameteri": lambda target, name, value: None, "glPixelStorei": lambda name, value: None,
            "glTexImage2D": lambda *args: None, "glTexSubImage2D": lambda *args: None,
            "glFlush": lambda: None, "glFinish": lambda: None,
        }
        if tex_storage:
            gl["glTexStorage2D"] = lambda target, levels, fmt, width, height: None
        self.gl = FakeLib(self.log, gl)

    def names(self):
        return [name for name, _ in self.log]

    def texture(self, width=2, height=3):
        return gl_texture.GlTexture(width, height, egl_library=self.egl, gles_library=self.gl)


class GlTextureTests(unittest.TestCase):
    def test_flip_rows(self):
        raster = bytes(range(2 * 3 * 4))  # 2 wide, 3 high: rows of 8 bytes
        flipped = gl_texture.flip_rows(raster, 2, 3)
        self.assertEqual(raster[16:24] + raster[8:16] + raster[0:8], flipped)
        self.assertEqual(raster, gl_texture.flip_rows(flipped, 2, 3))
        with self.assertRaises(ValueError):
            gl_texture.flip_rows(raster[:-1], 2, 3)

    def test_missing_library_is_a_gl_error(self):
        with self.assertRaises(gl_texture.GlError) as caught:
            gl_texture.GlTexture(4, 4, egl_library="no-such-libEGL.so.1")
        self.assertIn("not loadable", str(caught.exception))

    def test_missing_entry_point_is_a_gl_error(self):
        fake = FakeEgl()
        del fake.egl.eglGetCurrentContext
        with self.assertRaises(gl_texture.GlError):
            fake.texture()

    def test_choose_config_failure_is_checked(self):
        fake = FakeEgl(choose_ok=False)
        with self.assertRaises(gl_texture.GlError) as caught:
            fake.texture()
        self.assertIn("eglChooseConfig", str(caught.exception))
        self.assertNotIn("eglCreateContext", fake.names())
        self.assertNotIn("eglTerminate", fake.names())

    def test_context_is_destroyed_when_make_current_fails(self):
        fake = FakeEgl(make_current_ok=False)
        with self.assertRaises(gl_texture.GlError):
            fake.texture()
        destroyed = [args for name, args in fake.log if name == "eglDestroyContext"]
        self.assertEqual([(0x1000, 0x3003)], destroyed)
        self.assertNotIn("eglTerminate", fake.names())

    def test_es3_uses_sized_immutable_storage(self):
        fake = FakeEgl()
        texture = fake.texture()
        self.assertEqual(3, texture.client_version)
        self.assertIn("glTexStorage2D", fake.names())
        self.assertNotIn("glTexImage2D", fake.names())
        storage = [args for name, args in fake.log if name == "glTexStorage2D"][0]
        self.assertEqual((gl_texture.GL_TEXTURE_2D, 1, gl_texture.GL_RGBA8, 2, 3), storage)
        self.assertIn("TexStorage2D RGBA8", texture.info)
        texture.close()

    def test_es2_falls_back_to_tex_image_with_a_sized_format(self):
        fake = FakeEgl(versions=(2,))
        texture = fake.texture()
        self.assertEqual(2, texture.client_version)
        images = [args for name, args in fake.log if name == "glTexImage2D"]
        self.assertEqual(gl_texture.GL_RGBA8, images[0][2])
        texture.upload(bytes(2 * 3 * 4))
        self.assertNotIn("glBindBuffer", fake.names())  # no pixel unpack buffers in ES2
        texture.close()

    def test_upload_drains_errors_and_resets_unpack_state(self):
        fake = FakeEgl()
        texture = fake.texture()
        del fake.log[:]
        fake.gl_errors = [0x502, 0x500]  # left behind by SteamVR's copy
        fake.current = None              # someone released our context
        raster = bytes(range(24))
        with self.assertLogs("gl_texture", "DEBUG") as logs:
            texture.upload(raster)
        self.assertEqual(2, texture.stale_errors)
        self.assertTrue(any("left by SetOverlayTexture" in line for line in logs.output))
        self.assertEqual(texture.context, fake.current)
        calls = fake.log
        self.assertIn(("glBindBuffer", (gl_texture.GL_PIXEL_UNPACK_BUFFER, 0)), calls)
        for name in (gl_texture.GL_UNPACK_ROW_LENGTH, gl_texture.GL_UNPACK_SKIP_ROWS, gl_texture.GL_UNPACK_SKIP_PIXELS):
            self.assertIn(("glPixelStorei", (name, 0)), calls)
        self.assertIn(("glPixelStorei", (gl_texture.GL_UNPACK_ALIGNMENT, 4)), calls)
        sub = [args for name, args in calls if name == "glTexSubImage2D"][0]
        self.assertEqual(gl_texture.flip_rows(raster, 2, 3), sub[-1])
        self.assertLess(fake.names().index("glTexSubImage2D"), fake.names().index("glFlush"))
        texture.close()

    def test_upload_error_is_raised(self):
        fake = FakeEgl()
        texture = fake.texture()

        def failing_sub_image(*args):
            fake.gl_errors.append(0x501)

        fake.gl.glTexSubImage2D.impl = failing_sub_image
        with self.assertRaises(gl_texture.GlError):
            texture.upload(bytes(24))
        texture.close()

    def test_calls_from_another_thread_are_refused(self):
        fake = FakeEgl()
        texture = fake.texture()
        errors = []

        def worker():
            for call in (lambda: texture.upload(bytes(24)), texture.finish, texture.close):
                try:
                    call()
                except gl_texture.GlError as e:
                    errors.append(e)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(3, len(errors))
        self.assertEqual(5, texture.texture)  # untouched
        texture.close()

    def test_close_keeps_the_display_unless_asked(self):
        fake = FakeEgl()
        texture = fake.texture()
        texture.finish()
        texture.close()
        self.assertIn("glFinish", fake.names())
        self.assertIn("glDeleteTextures", fake.names())
        self.assertIn("eglDestroyContext", fake.names())
        self.assertNotIn("eglTerminate", fake.names())
        self.assertIsNone(fake.current)
        texture.close()  # twice is harmless
        other = FakeEgl().texture()
        other.close(terminate=True)
        self.assertIsNone(other.display)


@unittest.skipUnless(os.environ.get("FCAM_TEST_GL") == "1", "set FCAM_TEST_GL=1 (Linux with Mesa EGL)")
class GlSmokeTests(unittest.TestCase):
    """A real surfaceless EGL context: FCAM_TEST_GL=1 python3 -m unittest discover -s tests"""

    def test_real_context_uploads_and_closes(self):
        try:
            texture = gl_texture.GlTexture(64, 32)
        except gl_texture.GlError as e:
            if "not loadable" in str(e):
                self.skipTest(str(e))
            raise
        try:
            self.assertTrue(texture.texture)
            for value in (0, 128, 255):
                texture.upload(bytes((value, 64, 32, 255)) * (64 * 32))
            texture.finish()
        finally:
            texture.close()
        self.assertIsNone(texture.context)
        self.assertIsNotNone(texture.display)  # not terminated


# ----------------------------------------------------------------------------- openvr_min

class FakeTable:
    def __init__(self, results=None):
        self.results = results or {}
        self.created = {}
        self.calls = []

    def fn(self, index, restype, *argtypes):
        self.created[index] = (restype, argtypes)

        def call(*args):
            self.calls.append((index, args))
            return self.results.get(index, 0)
        return call


class OpenVRBindingTests(unittest.TestCase):
    def overlay(self, results=None):
        table = FakeTable(results)
        requested = []

        def get_interface(lib, versions, count):
            requested.append((versions, count))
            return table, versions[0]

        with mock.patch.object(openvr_min, "get_interface", get_interface):
            overlay = openvr_min.Overlay(None)
        self.assertEqual([(("IVROverlay_028",), 82)], requested)
        return overlay, table

    def test_constants_match_openvr_capi_h(self):
        self.assertEqual(82, openvr_min.IVROverlay_Count)
        self.assertEqual(("IVROverlay_028",), openvr_min.IVROverlay_Versions)
        self.assertEqual(("IVRSystem_026",), openvr_min.IVRSystem_Versions)
        self.assertEqual(("IVRApplications_008",), openvr_min.IVRApplications_Versions)
        self.assertEqual((303, 304), (openvr_min.VREvent_FocusEnter, openvr_min.VREvent_FocusLeave))
        self.assertEqual((500, 501, 700), (openvr_min.VREvent_OverlayShown, openvr_min.VREvent_OverlayHidden,
                                           openvr_min.VREvent_Quit))
        self.assertEqual(1204, openvr_min.VREvent_KeyboardClosed_Global)
        self.assertEqual((10, 11, 23, 34), (openvr_min.VROverlayError_UnknownOverlay,
                                            openvr_min.VROverlayError_InvalidHandle,
                                            openvr_min.VROverlayError_RequestFailed,
                                            openvr_min.VROverlayError_TimedOut))
        self.assertEqual((1, 1), (openvr_min.TextureType_OpenGL, openvr_min.ColorSpace_Gamma))

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit layout")
    def test_texture_struct_layout(self):
        self.assertEqual(16, ctypes.sizeof(openvr_min.Texture_t))
        self.assertEqual(16, ctypes.sizeof(openvr_min.VRTextureBounds_t))

    def test_function_positions(self):
        overlay, table = self.overlay()
        expected = {0: "FindOverlay", 1: "CreateOverlay", 3: "DestroyOverlay", 8: "GetOverlayErrorNameFromEnum",
                    20: "SetOverlaySortOrder", 22: "SetOverlayWidthInMeters", 30: "SetOverlayTextureBounds",
                    45: "IsOverlayVisible", 48: "PollNextOverlayEvent", 60: "SetOverlayTexture",
                    61: "ClearOverlayTexture", 62: "SetOverlayRaw", 63: "SetOverlayFromFile",
                    66: "GetOverlayTextureSize", 67: "CreateDashboardOverlay", 75: "ShowKeyboardForOverlay",
                    76: "GetKeyboardText", 77: "HideKeyboard"}
        self.assertTrue(set(expected) <= set(table.created), set(expected) - set(table.created))
        self.assertLess(max(table.created), openvr_min.IVROverlay_Count)

        def last_index(call):
            call()
            return table.calls[-1][0]

        self.assertEqual(60, last_index(lambda: overlay.set_gl_texture(0x10, 5)))
        texture = table.calls[-1][1][1]._obj
        self.assertEqual((5, openvr_min.TextureType_OpenGL, openvr_min.ColorSpace_Gamma),
                         (texture.handle, texture.eType, texture.eColorSpace))
        self.assertEqual(61, last_index(lambda: overlay.clear_texture(0x10)))
        self.assertEqual(62, last_index(lambda: overlay.set_raw(0x10, bytearray(16), 2, 2)))
        self.assertEqual(63, last_index(lambda: overlay.set_from_file(0x10, "/tmp/x.png")))
        self.assertEqual(66, last_index(lambda: overlay.texture_size(0x10)))
        self.assertEqual(3, last_index(lambda: overlay.destroy_overlay(0x10)))
        self.assertEqual(20, last_index(lambda: overlay.set_sort_order(0x10, 1)))
        self.assertEqual(30, last_index(lambda: overlay.set_texture_bounds(0x10, 0, 0, 1, 1)))
        self.assertEqual(76, last_index(overlay.keyboard_text))
        self.assertEqual(77, last_index(overlay.hide_keyboard))
        self.assertEqual(75, last_index(lambda: overlay.show_keyboard(0x10, "PC address")))
        flags = table.calls[-1][1][3]
        self.assertEqual(openvr_min.KeyboardFlag_Modal, flags)
        self.assertFalse(flags & openvr_min.KeyboardFlag_Minimal)

    def test_teardown_calls_return_codes_and_checks_carry_them(self):
        overlay, table = self.overlay({61: 23, 3: 11, 60: 10})
        self.assertEqual(23, overlay.clear_texture(0x10))
        self.assertEqual(11, overlay.destroy_overlay(0x10))
        with self.assertRaises(openvr_min.OpenVRError) as caught:
            overlay.set_gl_texture(0x10, 5)
        self.assertEqual(10, caught.exception.code)
        self.assertIn("SetOverlayTexture", str(caught.exception))
        self.assertIsNone(openvr_min.OpenVRError("plain").code)

    def test_find_overlay_reports_its_error_code(self):
        overlay, table = self.overlay({0: 23})
        self.assertEqual((23, None), overlay.find_overlay_code("ottlabs.fcam.dashboard"))
        self.assertIsNone(overlay.find_overlay("ottlabs.fcam.dashboard"))
        self.assertEqual(0, table.calls[-1][0])
        overlay, table = self.overlay()
        self.assertEqual(0, overlay.find_overlay_code("ottlabs.fcam.dashboard")[0])

    def test_init_shuts_down_when_an_interface_is_missing(self):
        calls = []

        class Lib:
            def VR_InitInternal2(self, err, app_type, startup_info):
                calls.append("VR_Init")
                return 1

            def VR_ShutdownInternal(self):
                calls.append("VR_Shutdown")

        def missing(lib):
            raise openvr_min.OpenVRError("runtime supports none of IVROverlay_028")

        with mock.patch.object(openvr_min, "find_library", lambda library=None: (Lib(), "/fake/libopenvr_api.so")), \
                mock.patch.object(openvr_min, "_prototype", lambda lib: None), \
                mock.patch.object(openvr_min, "System", lambda lib: object()), \
                mock.patch.object(openvr_min, "Overlay", missing):
            session = openvr_min.Session()
            with self.assertRaises(openvr_min.OpenVRError):
                session.init()
        self.assertEqual(["VR_Init", "VR_Shutdown"], calls)
        self.assertIsNone(session.token)
        self.assertIsNone(session.system)

    def test_keyboard_event_payload(self):
        event = openvr_min.VREvent_t()
        event.eventType = openvr_min.VREvent_KeyboardCharInput
        struct.pack_into("<8sQQ", event.data, 0, b"ab", 42, 0x1234)
        self.assertEqual(("ab", 42, 0x1234), event.keyboard())


# ----------------------------------------------------------------------------- lock and upgrade

class LockTests(unittest.TestCase):
    def test_json_round_trip_and_legacy_pid(self):
        fields = {"pid": 4242, "version": "0.2.0", "texture_mode": "auto", "sink": "GL", "heartbeat": 1700000000}
        self.assertEqual(fields, fcam_overlay.parse_lock(fcam_overlay.lock_text(fields)))
        self.assertEqual({"pid": 1234, "legacy": True}, fcam_overlay.parse_lock("1234\n"))
        for junk in ("", "   ", "{", "[1]", '{"pid": "x"}'):
            self.assertIsNone(fcam_overlay.parse_lock(junk), junk)

    def test_update_rewrites_the_whole_file(self):
        lock = fcam_overlay.InstanceLock(os.devnull)
        lock.handle = io.StringIO()
        lock.update(pid=1, texture_mode="file", heartbeat=1)
        lock.update(sink="file", heartbeat=22)
        self.assertEqual({"pid": 1, "texture_mode": "file", "sink": "file", "heartbeat": 22},
                         fcam_overlay.parse_lock(lock.handle.getvalue()))
        lock.close()

    @unittest.skipIf(fcam_overlay.fcntl is None, "needs fcntl (Linux)")
    def test_real_lock_is_seen_by_a_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.lock")
            lock = fcam_overlay.InstanceLock(path)
            self.assertTrue(lock.acquire(pid=os.getpid(), texture_mode="gl"))
            try:
                self.assertFalse(fcam_overlay.InstanceLock(path).acquire())
                info = fcam_overlay.probe_lock(path)
                self.assertEqual((os.getpid(), "gl"), (info["pid"], info["texture_mode"]))
            finally:
                lock.close()
            self.assertIsNone(fcam_overlay.probe_lock(path))
            legacy = os.path.join(tmp, "legacy.lock")
            with open(legacy, "w") as fh:
                fh.write("777")
            import fcntl
            with open(legacy) as holder:
                fcntl.flock(holder, fcntl.LOCK_EX)
                self.assertEqual(777, fcam_overlay.probe_lock(legacy)["pid"])

    @unittest.skipIf(fcam_overlay.fcntl is None, "needs fcntl (Linux)")
    def test_probes_do_not_collide_and_a_starting_instance_outlasts_one(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.lock")
            with open(path, "w") as fh:
                fh.write('{"pid": 1, "texture_mode": "file"}')
            with open(path) as prober:   # another probe (--status, --start) holding its shared lock
                fcntl.flock(prober, fcntl.LOCK_SH | fcntl.LOCK_NB)
                self.assertIsNone(fcam_overlay.probe_lock(path))   # probes never see each other
                lock = fcam_overlay.InstanceLock(path)
                self.assertFalse(lock.acquire(pid=os.getpid()))   # an instance starting right now
                attempts = []

                def sleep(seconds):
                    attempts.append(seconds)
                    fcntl.flock(prober, fcntl.LOCK_UN)   # the probe is done

                self.assertTrue(fcam_overlay.acquire_instance_lock(lock, {"pid": os.getpid()}, sleep=sleep))
            try:
                self.assertEqual([fcam_overlay.LOCK_RETRY_SECONDS], attempts)
                self.assertEqual(os.getpid(), fcam_overlay.probe_lock(path)["pid"])
            finally:
                lock.close()

    def test_a_starting_instance_retries_the_lock_for_about_a_second(self):
        results = iter([False, False, True])

        class Lock:
            def acquire(self, **fields):
                return next(results)

        slept = []
        self.assertTrue(fcam_overlay.acquire_instance_lock(Lock(), {"pid": 1}, sleep=slept.append))
        self.assertEqual([fcam_overlay.LOCK_RETRY_SECONDS] * 2, slept)

        class Taken:
            tries = 0

            def acquire(self, **fields):
                Taken.tries += 1
                return False

        slept.clear()
        self.assertFalse(fcam_overlay.acquire_instance_lock(Taken(), {"pid": 1}, sleep=slept.append))
        self.assertEqual(fcam_overlay.LOCK_ATTEMPTS, Taken.tries)
        self.assertAlmostEqual(1.0, fcam_overlay.LOCK_ATTEMPTS * fcam_overlay.LOCK_RETRY_SECONDS)
        self.assertEqual(fcam_overlay.LOCK_ATTEMPTS - 1, len(slept))

    def test_default_lock_lives_in_the_runtime_dir(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
            self.assertEqual(os.path.join(tmp, "ottlabs.fcam.lock"), fcam_overlay.default_lock_path())

    def test_texture_mode_from_command_lines(self):
        mode = fcam_overlay.texture_mode_from_argv
        self.assertEqual("file", mode(["/usr/bin/python3", "./fcam_overlay.py"], "file"))
        self.assertEqual("raw", mode(["python3", "fcam_overlay.py", "--texture-mode", "raw"], "file"))
        self.assertEqual("raw", mode(["python3", "fcam_overlay.py", "--texture", "raw"], "file"))
        self.assertEqual("gl", mode(["--texture-mode=raw", "--texture-mode=gl"], "file"))
        self.assertEqual("file", mode(["--target", "10.0.0.2"], "file"))

    def test_upgrade_restarts_only_non_raw_instances(self):
        killed, relaunched = [], []
        alive = iter([True, True, False])

        def relaunch():
            relaunched.append(True)
            return "launched (pid 99)"

        note = fcam_overlay.restart_instance({"pid": 42, "texture_mode": "gl"}, relaunch,
                                             kill=lambda pid, sig: killed.append((pid, sig)),
                                             alive=lambda pid: next(alive), sleep=lambda s: None)
        self.assertEqual([(42, signal.SIGTERM)], killed)
        self.assertEqual([True], relaunched)
        self.assertIn("restarted", note)
        for info in ({"pid": 42, "texture_mode": "raw"}, {"pid": 42, "legacy": True}):
            killed.clear()
            with mock.patch.object(fcam_overlay, "process_cmdline", lambda pid: None):
                note = fcam_overlay.restart_instance(info, relaunch, kill=lambda pid, sig: killed.append(pid))
            self.assertEqual([], killed)
            self.assertIn("reboot the headset", note)

    def legacy_mode(self, argv, panel_file=False):
        with mock.patch.object(fcam_overlay, "process_cmdline", lambda pid: argv), \
                mock.patch.object(fcam_overlay.os.path, "exists",
                                  lambda path: panel_file and path == "/dev/shm/fcam-panel-5.png"):
            return fcam_overlay.instance_texture_mode({"pid": 5, "legacy": True})

    def test_legacy_instance_mode_needs_positive_evidence(self):
        bare = ["/usr/bin/python3", "./fcam_overlay.py"]   # fcam_overlay.sh of every 0.1.x build
        # 5bb778d and 26d7b37 (0.1.0) had no --texture-mode and always used SetOverlayRaw
        self.assertIsNone(self.legacy_mode(bare))
        self.assertEqual("file", self.legacy_mode(bare, panel_file=True))   # only file-mode 0.1.x writes it
        self.assertEqual("raw", self.legacy_mode(bare + ["--texture-mode", "raw"], panel_file=True))
        self.assertEqual("file", self.legacy_mode(bare + ["--texture-mode", "file"]))
        # a pid that is not the overlay at all is never taken for one
        self.assertIsNone(self.legacy_mode(["/usr/bin/sleep", "100"], panel_file=True))
        self.assertIsNone(self.legacy_mode(["python3", "other.py", "--texture-mode", "file"]))
        self.assertIsNone(self.legacy_mode(None))

    def test_a_bare_legacy_instance_is_not_stopped(self):
        killed = []
        with mock.patch.object(fcam_overlay, "process_cmdline", lambda pid: ["/usr/bin/python3", "./fcam_overlay.py"]), \
                mock.patch.object(fcam_overlay.os.path, "exists", lambda path: False):
            note = fcam_overlay.restart_instance({"pid": 4242, "legacy": True}, lambda: self.fail("relaunched"),
                                                 kill=lambda pid, sig: killed.append((pid, sig)))
        self.assertEqual([], killed)
        self.assertIn("reboot the headset", note)
        self.assertIn("SetOverlayRaw", note)

    def test_a_pid_from_steamvr_is_only_stopped_when_it_is_the_overlay(self):
        killed, launched = [], []

        class Apps:
            version = "IVRApplications_008"

            def add_manifest(self, path):
                pass

            def set_auto_launch(self, key, value):
                pass

            def get_auto_launch(self, key):
                return True

            def process_id(self, key):
                return 4242

            def launch(self, key):
                launched.append(key)

        class Session:
            def __init__(self, library=None):
                self.applications = Apps()

            def init(self, app_type):
                return self

            def shutdown(self):
                pass

        for argv in (["/usr/bin/sleep", "100"], ["/usr/bin/python3", "./fcam_overlay.py"]):
            with mock.patch.object(fcam_overlay.ovr, "Session", Session), \
                    mock.patch.object(fcam_overlay, "running_instance", lambda lock_path=None: None), \
                    mock.patch.object(fcam_overlay, "process_alive", lambda pid: True), \
                    mock.patch.object(fcam_overlay, "process_cmdline", lambda pid: argv), \
                    mock.patch.object(fcam_overlay.os.path, "exists", lambda path: False), \
                    mock.patch.object(fcam_overlay.os, "kill", lambda pid, sig: killed.append(pid)):
                note = fcam_overlay.register_live(True, launch=True, library=None)
            self.assertEqual([], killed, argv)
            self.assertEqual([], launched, argv)
            self.assertIn("reboot the headset", note)

    def uninstall(self, instance=None, argv=None, panel_file=False, steamvr=True, kill=None,
                  error=None, steamvr_processes=False):
        """register_live(False) with SteamVR reporting pid 4242: (pids signalled, note, deregistered).
        steamvr=False: VR_Init raises error (default: no server, 121); steamvr_processes: what the
        /proc scan for vrserver/vrcompositor finds (True, False, or None: cannot tell)."""
        killed, deregistered = [], []
        error = error or openvr_min.OpenVRError("VR_Init failed (121)", 121)

        class Apps:
            def process_id(self, key):
                return 4242

            def set_auto_launch(self, key, value):
                deregistered.append(("autolaunch", value))

            def remove_manifest(self, path):
                deregistered.append(("manifest", path))

        class Session:
            def __init__(self, library=None):
                self.applications = Apps()

            def init(self, app_type):
                if not steamvr:
                    raise error
                return self

            def shutdown(self):
                pass

        def record(pid, sig):
            killed.append(pid)
            if kill is not None:
                kill(pid, sig)

        with mock.patch.object(fcam_overlay.ovr, "Session", Session), \
                mock.patch.object(fcam_overlay, "running_instance", lambda lock_path=None: instance), \
                mock.patch.object(fcam_overlay, "process_cmdline", lambda pid: argv), \
                mock.patch.object(fcam_overlay, "steamvr_process_running", lambda proc="/proc": steamvr_processes), \
                mock.patch.object(fcam_overlay.os.path, "exists",
                                  lambda path: panel_file and path.startswith("/dev/shm/fcam-panel-")), \
                mock.patch.object(fcam_overlay.os, "kill", record):
            note = fcam_overlay.register_live(False, launch=False, library=None)
        return killed, note, deregistered

    def test_uninstall_stops_only_an_overlay_process(self):
        bare = ["/usr/bin/python3", "./fcam_overlay.py"]
        killed, note, deregistered = self.uninstall(argv=["/usr/bin/sleep", "100"])
        self.assertEqual([], killed)
        self.assertEqual([("autolaunch", False), ("manifest", fcam_overlay.MANIFEST)], deregistered)
        # only with evidence of a mode that exits safely
        self.assertEqual([4242], self.uninstall(argv=bare + ["--texture-mode", "file"])[0])
        self.assertEqual([4242], self.uninstall(argv=bare, panel_file=True)[0])   # a file-mode 0.1.x build
        self.assertEqual([7], self.uninstall(instance={"pid": 7, "texture_mode": "gl", "lock": "x"}, argv=None)[0])
        # a pid from this version's own lock needs no command-line check
        self.assertTrue(fcam_overlay.is_overlay_pid(7, {"pid": 7, "texture_mode": "file"}))
        with mock.patch.object(fcam_overlay, "process_cmdline", lambda pid: None):
            self.assertFalse(fcam_overlay.is_overlay_pid(7, {"pid": 7, "legacy": True}))

    def test_uninstall_leaves_a_raw_or_unknown_instance_running_while_steamvr_runs(self):
        bare = ["/usr/bin/python3", "./fcam_overlay.py"]
        for instance, argv in (({"pid": 7, "texture_mode": "raw", "lock": "x"}, None),
                               (None, bare),                                  # 0.1.0: always SetOverlayRaw
                               ({"pid": 4242, "legacy": True, "lock": "y"}, bare),
                               (None, bare + ["--texture-mode", "raw", "--unsafe-raw"])):
            killed, note, deregistered = self.uninstall(instance=instance, argv=argv)
            self.assertEqual([], killed, (instance, argv))
            self.assertIn("left running", note)
            self.assertIn("reboot the headset", note)
            self.assertEqual([("autolaunch", False), ("manifest", fcam_overlay.MANIFEST)], deregistered)
        # without SteamVR no compositor holds its buffer: then it is stopped
        killed, note, _ = self.uninstall(instance={"pid": 7, "texture_mode": "raw", "lock": "x"}, steamvr=False)
        self.assertEqual([7], killed)
        self.assertIn("stopped pid 7", note)
        self.assertIn("SteamVR not running", note)

    def test_uninstall_takes_a_failed_vr_init_for_a_stopped_steamvr_only_with_proof(self):
        """Any VR_Init failure but 'no server' with no SteamVR process left may come from a running
        SteamVR: then a raw or unknown-mode instance is left running, as when SteamVR answers."""
        bare = ["/usr/bin/python3", "./fcam_overlay.py"]
        raw = {"pid": 7, "texture_mode": "raw", "lock": "x"}
        legacy = {"pid": 4242, "legacy": True, "lock": "y"}
        cases = (
            # a pinned interface the runtime lacks: raised after VR_Init worked, so SteamVR runs
            (raw, None, openvr_min.OpenVRError("runtime supports none of IVRApplications_008"), False),
            (raw, None, openvr_min.OpenVRError("libopenvr_api.so not found"), False),
            # another VR_Init error (IPC, HMD, ...) with a 0.1.0 instance (bare command line, maybe raw)
            (legacy, bare, openvr_min.OpenVRError("VR_Init failed (109)", 109), False),
            # "no server", but a vrcompositor is still there (SteamVR stopping or starting)
            (raw, None, openvr_min.OpenVRError("VR_Init failed (121)", 121), True),
            # "no server" and /proc cannot be read: not proven either
            (legacy, bare, openvr_min.OpenVRError("VR_Init failed (121)", 121), None),
        )
        for instance, argv, error, processes in cases:
            killed, note, _ = self.uninstall(instance=instance, argv=argv, steamvr=False, error=error,
                                             steamvr_processes=processes)
            self.assertEqual([], killed, (str(error), processes))
            self.assertIn("SteamVR not reachable", note)
            self.assertIn("left running", note)
            self.assertIn("reboot the headset", note)
        # a mode that exits safely is still stopped then
        killed, note, _ = self.uninstall(instance={"pid": 7, "texture_mode": "file", "lock": "x"}, steamvr=False,
                                         error=openvr_min.OpenVRError("VR_Init failed (109)", 109))
        self.assertEqual([7], killed)
        self.assertIn("stopped pid 7", note)

    def test_steamvr_is_certainly_down_only_on_no_server_without_steamvr_processes(self):
        no_server = openvr_min.OpenVRError("VR_Init failed (121)", openvr_min.VRInitError_Init_NoServerForBackgroundApp)
        for processes, down in ((False, True), (True, False), (None, False)):
            with mock.patch.object(fcam_overlay, "steamvr_process_running", lambda proc="/proc": processes):
                self.assertEqual(down, fcam_overlay.steamvr_certainly_down(no_server), processes)
                self.assertFalse(fcam_overlay.steamvr_certainly_down(openvr_min.OpenVRError("x")))
                self.assertFalse(fcam_overlay.steamvr_certainly_down(openvr_min.OpenVRError("x", 109)))

    def test_steamvr_processes_are_found_by_their_comm(self):
        with tempfile.TemporaryDirectory() as proc:
            for pid, comm in (("1", "systemd"), ("40", "gamescope"), ("41", "python3")):
                os.makedirs(os.path.join(proc, pid))
                with open(os.path.join(proc, pid, "comm"), "w") as fh:
                    fh.write(comm + "\n")
            os.makedirs(os.path.join(proc, "self"))
            self.assertIs(False, fcam_overlay.steamvr_process_running(proc))
            for pid, comm in (("50", "vrcompositor"), ("51", "vrserver")):
                os.makedirs(os.path.join(proc, pid))
                with open(os.path.join(proc, pid, "comm"), "w") as fh:
                    fh.write(comm + "\n")
                self.assertIs(True, fcam_overlay.steamvr_process_running(proc), comm)
                shutil.rmtree(os.path.join(proc, pid))
            self.assertIsNone(fcam_overlay.steamvr_process_running(os.path.join(proc, "missing")))

    def test_uninstall_of_an_instance_that_exits_meanwhile(self):
        def gone(pid, sig):
            raise ProcessLookupError(3, "No such process")

        for steamvr in (True, False):
            killed, note, _ = self.uninstall(instance={"pid": 7, "texture_mode": "file", "lock": "x"},
                                             steamvr=steamvr, kill=gone)
            self.assertEqual([7], killed)
            self.assertIn("pid 7 had already exited", note)

    def test_an_instance_that_exits_meanwhile_is_still_relaunched(self):
        relaunched = []

        def gone(pid, sig):
            raise ProcessLookupError(3, "No such process")

        def denied(pid, sig):
            raise PermissionError(1, "Operation not permitted")

        note = fcam_overlay.restart_instance({"pid": 42, "texture_mode": "file"},
                                             lambda: relaunched.append(True) or "launched (pid 99)", kill=gone)
        self.assertEqual([True], relaunched)
        self.assertIn("pid 42 had already exited", note)
        self.assertIn("launched (pid 99)", note)
        note = fcam_overlay.restart_instance({"pid": 42, "texture_mode": "file"}, lambda: self.fail("relaunched"),
                                             kill=denied)
        self.assertIn("cannot stop pid 42", note)

    def test_instance_that_does_not_exit_is_not_relaunched(self):
        clock = FakeClock()

        def sleep(seconds):
            clock.advance(seconds)

        note = fcam_overlay.restart_instance({"pid": 42, "texture_mode": "file"}, lambda: self.fail("relaunched"),
                                             kill=lambda pid, sig: None, alive=lambda pid: True, sleep=sleep,
                                             clock=clock)
        self.assertIn("did not exit", note)


class StartTests(unittest.TestCase):
    """--start: the recorded options, through SteamVR when it runs, headless otherwise."""

    def start(self, instance=None, steamvr=True, launch_fails=False, arguments="--texture-mode auto"):
        launched, detached = [], []

        class Apps:
            def launch(self, key):
                launched.append(key)
                if launch_fails:
                    raise openvr_min.OpenVRError("LaunchApplication failed", 101)

            def process_id(self, key):
                return 555

        class Session:
            def __init__(self, library=None):
                self.applications = Apps()

            def init(self, app_type):
                if not steamvr:
                    raise openvr_min.OpenVRError("VR_Init failed (121)", 121)
                return self

            def shutdown(self):
                pass

        def start_detached(run_args=()):
            detached.append(list(run_args))
            return 777

        def manifest_arguments(path=None):
            if isinstance(arguments, Exception):
                raise arguments
            return arguments

        with mock.patch.object(fcam_overlay.ovr, "Session", Session), \
                mock.patch.object(fcam_overlay, "running_instance", lambda lock_path=None: instance), \
                mock.patch.object(fcam_overlay, "manifest_arguments", manifest_arguments), \
                mock.patch.object(fcam_overlay, "start_detached", start_detached), \
                mock.patch.object(fcam_overlay.time, "sleep", lambda seconds: None):
            note = fcam_overlay.start_installed(None)
        return note, launched, detached

    def test_start_launches_through_steamvr(self):
        note, launched, detached = self.start()
        self.assertEqual([fcam_overlay.APP_KEY], launched)
        self.assertEqual([], detached)
        self.assertIn("launched (pid 555)", note)

    def test_start_falls_back_to_the_recorded_options_headless(self):
        for kwargs in ({"launch_fails": True}, {"steamvr": False}):
            note, launched, detached = self.start(**kwargs)
            self.assertEqual([["--texture-mode", "auto"]], detached, kwargs)
            self.assertIn("headless (pid 777)", note)

    def test_start_does_nothing_while_an_instance_runs(self):
        note, launched, detached = self.start(instance={"pid": 42, "texture_mode": "file"})
        self.assertEqual(([], []), (launched, detached))
        self.assertIn("already running (pid 42)", note)

    def test_an_unreadable_manifest_is_reported_not_raised(self):
        for error in (ValueError("Expecting value: line 1 column 1 (char 0)"), KeyError("applications"),
                      FileNotFoundError(2, "No such file or directory")):
            note, launched, detached = self.start(arguments=error)
            self.assertEqual(([], []), (launched, detached), error)
        self.assertIn("cannot read the options --install recorded", note)
        self.assertIn("install-overlay.sh", note)


class StopTests(unittest.TestCase):
    """--stop: frees the tracker port, but never stops an instance that may use raw mode."""

    def stop(self, instance, argv=None):
        killed = []
        with mock.patch.object(fcam_overlay, "running_instance", lambda lock_path=None: instance), \
                mock.patch.object(fcam_overlay, "process_cmdline", lambda pid: argv), \
                mock.patch.object(fcam_overlay, "process_alive", lambda pid: not killed), \
                mock.patch.object(fcam_overlay.os.path, "exists", lambda path: False), \
                mock.patch.object(fcam_overlay.os, "kill", lambda pid, sig: killed.append((pid, sig))):
            code, text = fcam_overlay.stop_running()
        return code, text, killed

    def test_stops_a_restartable_instance_and_waits_for_it(self):
        for mode in fcam_overlay.RESTARTABLE_MODES:
            code, text, killed = self.stop({"pid": 42, "texture_mode": mode})
            self.assertEqual(0, code, mode)
            self.assertEqual([(42, signal.SIGTERM)], killed)
            self.assertIn("stopped pid 42", text)
            self.assertIn("--start", text)

    def test_refuses_an_instance_that_may_use_raw_mode(self):
        bare = ["/usr/bin/python3", "./fcam_overlay.py"]
        for instance, argv in (({"pid": 42, "texture_mode": "raw"}, None),
                               ({"pid": 42, "legacy": True}, bare),     # 0.1.0: always SetOverlayRaw
                               ({"pid": 42, "legacy": True}, None),
                               ({"pid": 0}, None)):
            code, text, killed = self.stop(instance, argv)
            self.assertEqual(1, code, instance)
            self.assertEqual([], killed, instance)
            self.assertIn("unplug the tracker", text)

    def test_nothing_running(self):
        self.assertEqual((0, "no instance is running", []), self.stop(None))

    def test_an_instance_that_does_not_exit_is_reported(self):
        with mock.patch.object(fcam_overlay, "stop_instance",
                               lambda info: ("timeout", "pid 42 did not exit within 5 s after SIGTERM")):
            code, text, killed = self.stop({"pid": 42, "texture_mode": "file"})
        self.assertEqual(1, code)
        self.assertIn("did not exit", text)
        self.assertNotIn("--start", text)

    def test_status_warns_about_a_mode_that_may_be_raw(self):
        self.assertEqual("file", fcam_overlay.mode_text("file"))
        for mode in (None, "raw"):
            self.assertIn("may be raw", fcam_overlay.mode_text(mode))
            self.assertIn("--stop refuses it", fcam_overlay.mode_text(mode))
        self.assertTrue(fcam_overlay.mode_text(None).startswith("unknown"))


class LogFileTests(unittest.TestCase):
    """Only the instance that holds the lock rotates and opens the log file."""

    LIMIT = 1024 * 1024

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = os.path.join(tmp.name, "fcam_overlay.log")
        with open(self.log, "wb") as fh:
            fh.write(b"x" * (self.LIMIT + 1))
        self.lock = os.path.join(tmp.name, "test.lock")
        self.handlers = list(logging.getLogger().handlers)

    def main(self, acquired):
        with mock.patch.object(logging, "basicConfig"), \
                mock.patch.object(fcam_overlay.time, "sleep", lambda seconds: None), \
                mock.patch.object(fcam_overlay.InstanceLock, "acquire", lambda lock, **fields: acquired), \
                mock.patch.object(fcam_overlay, "probe_lock", lambda path: None), \
                mock.patch.object(fcam_overlay, "OverlayApp", mock.Mock(side_effect=OSError("address in use"))):
            return fcam_overlay.main(["--log-file", self.log, "--lock-file", self.lock])

    def test_a_duplicate_launch_leaves_the_log_alone(self):
        self.assertEqual(0, self.main(acquired=False))
        self.assertEqual(self.LIMIT + 1, os.path.getsize(self.log))
        self.assertFalse(os.path.exists(self.log + ".old"))
        self.assertEqual(self.handlers, logging.getLogger().handlers)

    def test_the_running_instance_rotates_and_writes_the_log(self):
        self.assertEqual(1, self.main(acquired=True))   # the bridge could not start (fake)
        self.assertEqual(self.LIMIT + 1, os.path.getsize(self.log + ".old"))
        with open(self.log, encoding="utf-8") as fh:
            self.assertIn("cannot start the bridge", fh.read())
        self.assertEqual(self.handlers, logging.getLogger().handlers)   # detached and closed again


class PackagingTests(unittest.TestCase):
    """Every file the overlay needs at run time has to reach each list that ships, copies or checks it
    (install-overlay.sh, gl_exit_test.sh, CI): without gl_texture.py, GL silently falls back to file."""

    def read(self, rel):
        with open(os.path.join(ROOT, *rel.split("/")), encoding="utf-8") as fh:
            return fh.read()

    def python_modules(self):
        """fcam_overlay.py and the repository's modules it imports, directly or indirectly."""
        found, todo = {"overlay/fcam_overlay.py"}, ["overlay/fcam_overlay.py"]
        while todo:
            for name in re.findall(r"(?m)^\s*(?:import|from)\s+(\w+)", self.read(todo.pop())):
                for rel in ("overlay/%s.py" % name, "%s.py" % name):
                    if rel not in found and os.path.exists(os.path.join(ROOT, *rel.split("/"))):
                        found.add(rel)
                        todo.append(rel)
        return found

    def manifest_files(self):
        import json
        app = json.loads(self.read("overlay/fcam.vrmanifest"))["applications"][0]
        return {"overlay/fcam.vrmanifest", "overlay/" + app["binary_path_linux_arm"], "overlay/" + app["image_path"]}

    @staticmethod
    def install_sources(text, prefixes):
        """Repository paths of the sources of every install -m command; prefixes maps a quoted
        "$var/" prefix (or a whole "$var") to the repository directory (or file) it stands for."""
        sources = set()
        for line in re.sub(r"\\\n\s*", " ", text).splitlines():
            if not line.strip().startswith("install -m"):
                continue
            for token in re.findall(r'"([^"]+)"', line):
                for prefix, target in prefixes.items():
                    if token == prefix:
                        sources.add(target)
                    elif prefix.endswith("/") and token.startswith(prefix):
                        path = os.path.normpath(os.path.join(target, token[len(prefix):]))
                        sources.add(path.replace(os.sep, "/"))
        return sources

    def test_the_overlay_imports_what_the_lists_must_carry(self):
        modules = self.python_modules()
        for rel in ("overlay/gl_texture.py", "overlay/openvr_min.py", "overlay/fcam_font.py", "fcam_bridge.py"):
            self.assertIn(rel, modules)

    def test_install_overlay_installs_every_runtime_file(self):
        shipped = self.install_sources(self.read("overlay/install-overlay.sh"), {"$here/": "overlay"})
        needed = self.python_modules() | self.manifest_files()
        self.assertEqual(set(), needed - shipped)

    def test_gl_exit_test_copies_and_checks_every_runtime_file(self):
        text = self.read("overlay/tools/gl_exit_test.sh")
        self.assertRegex(text, r'(?m)^bridge="\$\(cd "\$src/\.\." && pwd\)/fcam_bridge\.py"$')
        copied = self.install_sources(text, {"$src/": "overlay", "$bridge": "fcam_bridge.py"})
        # the test runs fcam_overlay.py directly: the launcher script is not needed there
        needed = self.python_modules() | self.manifest_files() - {"overlay/fcam_overlay.sh"}
        self.assertEqual(set(), needed - copied)
        checked = re.search(r'(?m)^for f in (.*); do\n\s*\[ -f "\$src/\$f" \]', text).group(1).split()
        self.assertEqual(set(), {rel for rel in needed if rel.startswith("overlay/")} - {"overlay/" + f for f in checked})

    def test_ci_byte_compiles_every_module(self):
        ci = self.read(".forgejo/workflows/ci.yml")
        compiled = set(re.search(r"python3 -m py_compile ((?:[^\n\\]|\\\n)+)", ci).group(1).replace("\\", " ").split())
        self.assertEqual(set(), self.python_modules() - compiled)


# ----------------------------------------------------------------------------- on-headset exit test

class GlExitTestScriptTests(unittest.TestCase):
    """overlay/tools/gl_exit_test.sh only runs on the headset; these check what it promises."""

    def setUp(self):
        with open(os.path.join(ROOT, "overlay", "tools", "gl_exit_test.sh"), encoding="utf-8") as fh:
            self.text = fh.read()
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            self.readme = fh.read()

    def function(self, name):
        match = re.search(r"^%s\(\)\s*\{(.*?)^\}" % re.escape(name), self.text, re.M | re.S)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def blocks(self):
        """{name: [(mode, signal, seconds, runs)]} from the block calls at the end of the script."""
        found = {}
        for name, mode, sig, secs, runs in re.findall(r"(?m)^block (\S+) (\S+) (\S+) (\d+) (\d+)", self.text):
            found.setdefault(name, []).append((mode, sig, int(secs), int(runs)))
        return found

    def test_the_matrix_can_reach_the_pass_criterion(self):
        blocks = self.blocks()
        matrix = re.search(r'(?m)^MATRIX="([^"]+)"', self.text).group(1).split()
        self.assertEqual(["A", "B1", "B2", "C", "D"], matrix)
        gl = sum(runs for name in matrix for mode, _, _, runs in blocks[name] if mode == "gl")
        kills = sum(runs for name in matrix for mode, sig, _, runs in blocks[name] if mode == "gl" and sig == "KILL")
        self.assertGreaterEqual(gl, 35)
        self.assertGreaterEqual(kills, 10)
        # the stated numbers are the ones the matrix produces, and PASS is not a literal threshold
        self.assertIn("which is %d GL exits" % gl, self.text)
        self.assertIn("0 crashes over %d GL exits" % gl, self.readme)
        self.assertNotRegex(self.function("summary"), r"gl_exits\D+-ge \d")
        # the README table has the script's run counts
        for name in matrix + ["F"]:
            runs = sum(entry[3] for entry in blocks[name])
            self.assertRegex(self.readme, r"(?m)^\| %s \|.*\| %d \|$" % (re.escape(name), runs), name)

    def test_the_installed_overlay_is_stopped_only_in_a_restartable_mode(self):
        probe = self.function("installed_instance")
        self.assertIn("instance_texture_mode(info)", probe)
        self.assertIn("RESTARTABLE_MODES", probe)
        refused = self.text.index('[ "$irestartable" != yes ]')
        stopped = self.text.index('kill -TERM "$ipid"')
        self.assertLess(refused, stopped)
        self.assertLess(self.text.index('read -r ipid imode irestartable <<<"$(installed_instance)"'), refused)

    def test_runs_that_need_the_tab_count_only_with_the_tab_open(self):
        run_one = self.function("run_one")
        waited = run_one.index('wait_tab_visible "$logf" 60')
        observed = run_one.index('observe "$block" "$secs"', waited)
        rechecked = run_one.index('[ "$(tab_state "$logf")" = visible ] || uncounted=')
        stopped = run_one.index('stop_overlay "$sig"')
        self.assertLess(waited, observed)
        self.assertLess(observed, rechecked)
        self.assertLess(rechecked, stopped)
        not_counted = run_one.index("return 2")
        self.assertLess(not_counted, run_one.index('passed[$block]='))
        self.assertIn("2) ;;", self.function("block"))   # a run that was not counted is repeated
        self.assertIn('[ "$1" = B2 ] || [ "$1" = F ]', self.function("needs_tab"))

    def test_block_f_is_part_of_the_script(self):
        blocks = self.blocks()
        self.assertEqual({"gl", "file"}, {mode for mode, _, _, _ in blocks["F"]})
        self.assertNotIn("F", re.search(r'(?m)^blocks="([^"]+)"', self.text).group(1).split(","))
        self.assertIn("proc_sample", self.function("observe"))
        self.assertIn("gl_exit_test.sh --blocks F", self.readme)
        self.assertNotIn("python3 ~/fcam-gltest/fcam_overlay.py", self.readme)   # no second instance by hand

    def test_standby_and_a_steamvr_restart_are_not_counted_as_failures(self):
        run_one = self.function("run_one")
        started = run_one.index('start_overlay "$mode"')
        crash = run_one.index('if [ -n "$lines" ]; then', started)
        restart = run_one.index('gone="$(steamvr_gone "$logf")"')
        standby = run_one.index('elif grep -q "compositor in standby" "$logf"')
        passed = run_one.index('passed[$block]=')
        self.assertLess(crash, restart)     # journal evidence of a crash always wins
        self.assertLess(restart, standby)
        self.assertLess(standby, passed)
        self.assertIn("return 3", run_one[restart:standby])
        self.assertIn("return 2", run_one[standby:passed])
        # compared with the session at the script's start, not one taken per run
        self.assertNotIn("inv=", run_one)
        top = self.text[self.text.index('\nVRC="$(pids vrcompositor)"'):self.text.index("\ntrap cleanup EXIT")]
        self.assertIn('INV0="$(steamvr_invocation)"', top)
        self.assertIn('SVC0="$(steamvr_state)"', top)
        # a shutdown is: a new invocation, the unit no longer active, no compositor, or a quit request
        gone = self.function("steamvr_gone")
        self.assertIn('[ "$(steamvr_invocation)" != "$INV0" ]', gone)
        self.assertIn('[ "$SVC0" = active ] && [ "$now" != active ]', gone)
        self.assertIn('[ -z "$(pids vrcompositor)" ]', gone)
        self.assertIn('grep -q "SteamVR requested quit"', gone)
        with open(os.path.join(ROOT, "overlay", "fcam_overlay.py"), encoding="utf-8") as fh:
            self.assertIn('log.info("SteamVR requested quit")', fh.read())
        # before every run (after the B2/F prompt): a changed session stops unfinished, unless the
        # journal shows a crash since the last check
        before = run_one[:started]
        self.assertIn('crashed="$(compositor_changes)"', before)
        self.assertIn('gone="$(steamvr_gone)"', before)
        self.assertIn('lines="$(crash_lines "$checked_until")"', before)
        self.assertLess(before.index("return 1"), before.index("return 3"))
        self.assertLess(self.function("block").index("ask_for_tab"), self.function("block").index("run_one"))
        self.assertLess(run_one.index('checked_until="$(date'), run_one.index('lines="$(crash_lines "$t")"'))
        # a NOT FINISHED result says when the system journal could not be read
        self.assertIn('[ "$journal_ok" = 0 ]', self.function("summary"))
        self.assertIn("3) exit 3 ;;", self.function("block"))
        self.assertIn('[ -z "$unfinished" ] || exit 3', self.function("cleanup"))
        # "compositor in standby" is what the overlay logs for RequestFailed/TimedOut
        with open(os.path.join(ROOT, "overlay", "fcam_overlay.py"), encoding="utf-8") as fh:
            self.assertIn("compositor in standby?", fh.read())
        self.assertIn("awake", self.readme[self.readme.index("## Testing the GL texture path"):])

    def test_blocks_that_need_the_tab_refuse_to_start_without_a_terminal(self):
        refused = self.text.index('if [ ! -t 0 ] && [ "$prompt" = 1 ] && { wants B2 || wants F; }; then')
        self.assertLess(refused, self.text.index('kill -TERM "$ipid"'))   # before anything is stopped
        self.assertLess(refused, self.text.index("\ntrap cleanup EXIT"))
        section = self.readme[self.readme.index("## Testing the GL texture path"):]
        section = section[:section.index("\n## ", 3)]
        self.assertIn("ssh -t", section)

    def test_printed_hints_name_this_copy(self):
        for line in self.text.splitlines():
            if not line.lstrip().startswith("#"):
                self.assertNotIn("~/Babble-Bridge", line)
        self.assertIn("bash $src/install-overlay.sh", self.text)


# ----------------------------------------------------------------------------- bridge snapshot

class BridgeSnapshotTests(unittest.TestCase):
    def test_snapshot_while_subscribers_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tracker.etvr")
            with open(path, "wb") as fh:
                fh.write(b"\xff\xd8\xff\xd9")
            args = fcam_bridge.build_parser().parse_args(["--serial", path, "--listen", "127.0.0.1:0"])
            runtime = fcam_bridge.BridgeRuntime(args)
            self.addCleanup(runtime.bridge.sock.close)
            self.addCleanup(runtime.wake_r.close)
            self.addCleanup(runtime.wake_w.close)
            bridge = runtime.bridge
            for index in range(300):
                bridge._subscribe(("10.0.%d.%d" % (index // 250, index % 250), 5000), "base")
            done = threading.Event()

            def churn():
                try:
                    for round_ in range(3000):
                        key = ("10.9.%d.%d" % (round_ % 200, round_ % 7), 6000)
                        bridge._subscribe(key, "churn")
                        bridge._unsubscribe(key)
                        bridge._expire_subscribers(time.monotonic() - 10)
                finally:
                    done.set()

            previous = sys.getswitchinterval()
            sys.setswitchinterval(1e-6)
            try:
                with self.assertLogs("fcam", "INFO"):
                    thread = threading.Thread(target=churn)
                    thread.start()
                    snapshots = 0
                    while not done.is_set():
                        snapshot = runtime.snapshot()  # raised RuntimeError when it iterated self.subs
                        snapshots += 1
                    thread.join()
            finally:
                sys.setswitchinterval(previous)
            self.assertGreater(snapshots, 0)
            self.assertGreaterEqual(len(snapshot["subscribers"]), 300)

    def test_version_is_the_release_tag(self):
        self.assertEqual("0.2.0", fcam_bridge.__version__)


if __name__ == "__main__":
    unittest.main()
