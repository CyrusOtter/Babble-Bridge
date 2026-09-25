"""Unit tests for the FCAM bridge and overlay helpers. Standard library only:

    python3 -m unittest discover -s tests -v
"""
import json
import os
import socket
import struct
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "overlay"))

import fcam_bridge  # noqa: E402
import fcam_overlay  # noqa: E402
import openvr_min  # noqa: E402

FAKE_JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 3 + b"\xff\xd9"


def etvr(frame):
    return fcam_bridge.ETVR_HEADER + struct.pack("<H", len(frame)) + frame


class EtvrParserTests(unittest.TestCase):
    def feed_in_chunks(self, parser, data, sizes=(1, 7, 64, 3, 1000)):
        pos, i = 0, 0
        while pos < len(data):
            size = sizes[i % len(sizes)]
            parser.feed(data[pos:pos + size])
            pos += size
            i += 1

    def test_frames_and_text_are_separated(self):
        frames, text = [], []
        parser = fcam_bridge.EtvrParser(frames.append, text.append)
        stream = b"[123][E][SerialManager.cpp] boot" + etvr(FAKE_JPEG) * 2 + b"log line" + etvr(FAKE_JPEG)
        self.feed_in_chunks(parser, stream)
        self.assertEqual(3, parser.frames)
        self.assertEqual(0, parser.bad_frames)
        self.assertEqual([FAKE_JPEG] * 3, frames)
        self.assertIn(b"boot", b"".join(text))
        self.assertIn(b"log line", b"".join(text))

    def test_bad_frame_is_skipped_and_parser_resyncs(self):
        frames = []
        parser = fcam_bridge.EtvrParser(frames.append)
        broken = FAKE_JPEG[:-2] + b"\x00\x00"  # no EOI marker
        parser.feed(etvr(broken) + etvr(FAKE_JPEG))
        self.assertEqual(1, parser.bad_frames)
        self.assertEqual([FAKE_JPEG], frames)

    def test_length_that_claims_more_than_available_waits(self):
        frames = []
        parser = fcam_bridge.EtvrParser(frames.append)
        data = etvr(FAKE_JPEG)
        parser.feed(data[:20])
        self.assertEqual([], frames)
        parser.feed(data[20:])
        self.assertEqual([FAKE_JPEG], frames)

    def test_split_jpegs_fallback(self):
        raw = b"junk" + FAKE_JPEG + FAKE_JPEG + b"tail"
        self.assertEqual([FAKE_JPEG, FAKE_JPEG], fcam_bridge.split_jpegs(raw))


class DiscoveryTests(unittest.TestCase):
    @staticmethod
    def make_sysfs(root, nodes):
        """Fake sysfs: class/tty/<node>/device is a plain directory and the USB attributes live
        one level up, which exercises the same walk-up as the real interface/device layout."""
        for name, vid, pid, serial, product in nodes:
            base = os.path.join(root, "class", "tty", name)
            os.makedirs(os.path.join(base, "device"))
            if vid:
                for key, value in (("idVendor", vid), ("idProduct", pid), ("serial", serial), ("product", product)):
                    with open(os.path.join(base, key), "w") as fh:
                        fh.write(value + "\n")

    def test_finds_ports_by_usb_id_and_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysfs, dev = os.path.join(tmp, "sys"), os.path.join(tmp, "dev")
            self.make_sysfs(sysfs, [
                ("ttyACM0", "2341", "0043", "ARD1", "Arduino Uno"),
                ("ttyACM2", "303a", "1001", "AA:BB:CC:DD:EE:01", "USB JTAG/serial debug unit"),
                ("ttyACM1", "303A", "1001", "AA:BB:CC:DD:EE:02", "USB JTAG/serial debug unit"),
                ("ttyS0", None, None, None, None),
                ("ttyUSB0", "303a", "1001", "", "USB JTAG/serial debug unit"),
            ])
            ports = fcam_bridge.find_tracker_ports(sysfs=sysfs, dev=dev)
            self.assertEqual(["ttyACM1", "ttyACM2", "ttyUSB0"], [p["node"] for p in ports])
            self.assertEqual(os.path.join(dev, "ttyACM1"), ports[0]["path"])
            self.assertEqual("USB JTAG/serial debug unit", ports[0]["product"])
            pinned = fcam_bridge.find_tracker_ports(usb_serial="aa:bb:cc:dd:ee:01", sysfs=sysfs, dev=dev)
            self.assertEqual(["ttyACM2"], [p["node"] for p in pinned])
            self.assertEqual([], fcam_bridge.find_tracker_ports(usb_id="1234:5678", sysfs=sysfs, dev=dev))
            self.assertEqual([], fcam_bridge.find_tracker_ports(sysfs=os.path.join(tmp, "missing"), dev=dev))

    def test_auto_source_follows_the_unit_it_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysfs, dev = os.path.join(tmp, "sys"), os.path.join(tmp, "dev")
            wake_r, wake_w = socket.socketpair()
            self.addCleanup(wake_r.close)
            self.addCleanup(wake_w.close)
            frames = fcam_bridge.FrameQueue(wake_w)
            source = fcam_bridge.SerialSource("auto", 3000000, frames, __import__("threading").Event(), False,
                                              sysfs=sysfs, dev=dev)
            self.assertIsNone(source._discover())  # nothing plugged in
            self.assertTrue(source.description.startswith("waiting for usb 303a:1001"))
            self.make_sysfs(sysfs, [("ttyACM0", "303a", "1001", "UNIT-A", "tracker")])
            self.assertEqual(os.path.join(dev, "ttyACM0"), source._discover())
            self.assertEqual("UNIT-A", source.device_serial)
            # the tracker re-enumerates as ttyACM1 while another unit takes ttyACM0
            import shutil
            shutil.rmtree(os.path.join(sysfs, "class", "tty", "ttyACM0"))
            self.make_sysfs(sysfs, [("ttyACM0", "303a", "1001", "UNIT-B", "tracker"),
                                    ("ttyACM1", "303a", "1001", "UNIT-A", "tracker")])
            self.assertEqual(os.path.join(dev, "ttyACM1"), source._discover())

    def test_serial_defaults_to_auto(self):
        args = fcam_bridge.build_parser().parse_args([])
        self.assertEqual("auto", args.serial)
        self.assertEqual("303a:1001", args.usb_id)
        self.assertIsNone(args.usb_serial)


class ProtocolTests(unittest.TestCase):
    def test_header_round_trip(self):
        header = fcam_bridge.pack_header(fcam_bridge.TYPE_FRAME, fcam_bridge.CODEC_JPEG, 0, 65535, 3, 7, 1400,
                                         123456, 4200, 0xDEADBEEF)
        self.assertEqual(fcam_bridge.HEADER_LEN, len(header))
        self.assertEqual(b"FCAM", header[:4])
        fields = fcam_bridge.unpack_header(header)
        self.assertEqual((fcam_bridge.TYPE_FRAME, fcam_bridge.CODEC_JPEG, 0, 65535, 3, 7, 1400, 123456, 4200,
                          0xDEADBEEF), fields)

    def test_unpack_rejects_garbage(self):
        self.assertIsNone(fcam_bridge.unpack_header(b"XCAM" + b"\0" * 24))
        self.assertIsNone(fcam_bridge.unpack_header(b"FCAM\x02" + b"\0" * 23))
        self.assertIsNone(fcam_bridge.unpack_header(b"FCAM"))

    def test_parse_hostport(self):
        self.assertEqual(("0.0.0.0", 8555), fcam_bridge.parse_hostport("0.0.0.0:8555"))
        self.assertEqual(("0.0.0.0", 9000), fcam_bridge.parse_hostport(":9000"))
        self.assertEqual(("host", 8555), fcam_bridge.parse_hostport("host"))
        self.assertEqual(("fd00::1", 8556), fcam_bridge.parse_hostport("[fd00::1]:8556"))
        self.assertEqual(("fd00::1", 8555), fcam_bridge.parse_hostport("fd00::1"))
        with self.assertRaises(ValueError):
            fcam_bridge.parse_hostport("host:70000")


class BridgeEndToEndTests(unittest.TestCase):
    def test_replay_file_streams_to_a_subscriber(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tracker.etvr")
            with open(path, "wb") as fh:
                fh.write(b"[1][I] boot" + etvr(FAKE_JPEG) * 5)
            args = fcam_bridge.build_parser().parse_args(
                ["--serial", path, "--listen", "127.0.0.1:0", "--replay-fps", "60", "--chunk", "300",
                 "--status-interval", "0.2"])
            runtime = fcam_bridge.BridgeRuntime(args)
            port = runtime.bridge.listen_addr[1]
            self.assertGreater(port, 0)
            runtime.start()
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.bind(("127.0.0.1", 0))
            client.settimeout(0.2)
            try:
                subscribe = fcam_bridge.pack_header(fcam_bridge.TYPE_SUBSCRIBE) + b"unittest"
                frames, statuses = {}, []
                complete = []
                deadline = time.monotonic() + 3.0
                next_sub = 0.0
                while time.monotonic() < deadline and (len(complete) < 10 or not statuses):
                    if time.monotonic() >= next_sub:
                        client.sendto(subscribe, ("127.0.0.1", port))
                        next_sub = time.monotonic() + 0.5
                    try:
                        data, _ = client.recvfrom(65535)
                    except socket.timeout:
                        continue
                    fields = fcam_bridge.unpack_header(data)
                    self.assertIsNotNone(fields)
                    msg_type, codec, flags, seq, ci, cc, plen, flen, coff, _ts = fields
                    payload = data[fcam_bridge.HEADER_LEN:]
                    self.assertEqual(plen, len(payload))
                    if msg_type == fcam_bridge.TYPE_STATUS:
                        statuses.append(payload.decode())
                        continue
                    self.assertEqual(fcam_bridge.TYPE_FRAME, msg_type)
                    self.assertEqual(fcam_bridge.CODEC_JPEG, codec)
                    buf, got = frames.setdefault(seq, (bytearray(flen), set()))
                    buf[coff:coff + plen] = payload
                    got.add(ci)
                    if len(got) == cc:
                        complete.append(bytes(buf))
                        del frames[seq]
                client.sendto(fcam_bridge.pack_header(fcam_bridge.TYPE_UNSUBSCRIBE), ("127.0.0.1", port))
            finally:
                client.close()
                runtime.stop()
            self.assertGreaterEqual(len(complete), 10)
            self.assertTrue(all(frame == FAKE_JPEG for frame in complete))
            self.assertTrue(any("state=streaming" in s for s in statuses), statuses)
            snapshot = runtime.snapshot()
            self.assertEqual("streaming", snapshot["state"])
            self.assertEqual(port, snapshot["listen_port"])
            self.assertGreaterEqual(snapshot["frames"], 10)


class OverlayHelperTests(unittest.TestCase):
    def test_panel_draws_text_and_button(self):
        panel = fcam_overlay.Panel(fcam_overlay.PANEL_W, fcam_overlay.PANEL_H)
        snapshot = {"state": "streaming", "source": "/dev/ttyACM0", "fps": 60.0, "frames": 10, "bad": 0,
                    "dropped": 0, "unsent": 0, "send_errors": 0, "subscribers": ["10.0.0.2:5000"],
                    "listen_port": 8555, "uptime": 5, "opens": 1}
        fcam_overlay.draw_panel(panel, snapshot, False, "2.17.10")
        self.assertEqual(fcam_overlay.PANEL_W * fcam_overlay.PANEL_H * 4, len(panel.buf))
        background = bytes(fcam_overlay.BG)
        painted = sum(1 for i in range(0, len(panel.buf), 4) if panel.buf[i:i + 4] != background)
        self.assertGreater(painted, 10000)

    def test_panel_png_is_well_formed(self):
        import zlib
        panel = fcam_overlay.Panel(8, 4)
        panel.fill((10, 20, 30, 255))
        panel.fill((200, 100, 50, 255), 2, 1, 5, 3)
        png = panel.to_png()
        self.assertTrue(png.startswith(bytes((137, 80, 78, 71, 13, 10, 26, 10))))
        self.assertEqual(b"IHDR", png[12:16])
        width, height, depth, color = struct.unpack(">IIBB", png[16:26])
        self.assertEqual((8, 4, 8, 6), (width, height, depth, color))
        idat_len = struct.unpack(">I", png[33:37])[0]
        self.assertEqual(b"IDAT", png[37:41])
        raw = zlib.decompress(png[41:41 + idat_len])
        self.assertEqual(4 * (1 + 8 * 4), len(raw))
        self.assertEqual(bytes((200, 100, 50, 255)), raw[1 + 33 + 2 * 4:1 + 33 + 3 * 4])
        self.assertTrue(png.endswith(bytes((73, 69, 78, 68, 174, 66, 96, 130))))

    def test_button_hit_test_accepts_both_y_orientations(self):
        self.assertTrue(fcam_overlay.in_button(320, fcam_overlay.PANEL_H - 10))
        self.assertTrue(fcam_overlay.in_button(320, 10))
        self.assertFalse(fcam_overlay.in_button(320, fcam_overlay.PANEL_H // 2))
        self.assertFalse(fcam_overlay.in_button(-5, fcam_overlay.PANEL_H - 10))

    def test_register_files_adds_and_removes_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_config_dir, original_manifest = fcam_overlay.config_dir, fcam_overlay.MANIFEST
            fcam_overlay.config_dir = lambda: tmp
            fcam_overlay.MANIFEST = os.path.join(tmp, "fcam.vrmanifest")
            try:
                with open(os.path.join(tmp, "appconfig.json"), "w") as fh:
                    json.dump({"manifest_paths": ["/somewhere/steamapps.vrmanifest"]}, fh)
                appconfig, appcfg = fcam_overlay.register_files(True)
                with open(appconfig) as fh:
                    paths = json.load(fh)["manifest_paths"]
                self.assertEqual(["/somewhere/steamapps.vrmanifest", os.path.abspath(fcam_overlay.MANIFEST)], paths)
                with open(appcfg) as fh:
                    self.assertTrue(json.load(fh)["autolaunch"])
                fcam_overlay.register_files(False)
                with open(appconfig) as fh:
                    self.assertEqual(["/somewhere/steamapps.vrmanifest"], json.load(fh)["manifest_paths"])
                with open(appcfg) as fh:
                    self.assertFalse(json.load(fh)["autolaunch"])
            finally:
                fcam_overlay.config_dir, fcam_overlay.MANIFEST = original_config_dir, original_manifest


class OpenVRBindingTests(unittest.TestCase):
    def test_event_struct_matches_linux_layout(self):
        self.assertEqual(60, openvr_min.ctypes.sizeof(openvr_min.VREvent_t))
        event = openvr_min.VREvent_t()
        event.eventType = openvr_min.VREvent_MouseButtonDown
        struct.pack_into("<ffI", event.data, 0, 12.5, 300.0, 1)
        self.assertEqual((12.5, 300.0, 1), event.mouse())

    def test_missing_library_is_reported(self):
        original = openvr_min.LIBRARY_CANDIDATES, openvr_min.runtime_dirs
        openvr_min.LIBRARY_CANDIDATES = ()
        openvr_min.runtime_dirs = lambda: []
        env = os.environ.pop("FCAM_OPENVR_LIB", None)
        try:
            with self.assertRaises(openvr_min.OpenVRError):
                openvr_min.find_library(os.path.join(tempfile.gettempdir(), "no-such-libopenvr_api.so"))
        finally:
            openvr_min.LIBRARY_CANDIDATES, openvr_min.runtime_dirs = original
            if env is not None:
                os.environ["FCAM_OPENVR_LIB"] = env


if __name__ == "__main__":
    unittest.main()
