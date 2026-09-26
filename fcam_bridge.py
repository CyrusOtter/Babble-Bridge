#!/usr/bin/env python3
"""
fcam_bridge.py - forward a serial Babble / OpenIris face tracker to Baballonia
over the network with the FCAM/UDP protocol (see PROTOCOL.md; receiving side: Baballonia's
FCAM UDP Stream capture, https://github.com/Project-Babble/Baballonia).

    tracker --USB CDC-ACM--> /dev/ttyACM0 --this bridge--> UDP --> Baballonia
                                                                 (fcam://<host>:8555)

Uses only the Python 3 standard library, so it runs unmodified on a Steam Frame
(SteamOS, Python 3.12, no pip packages). It also runs on Windows/macOS for
testing when --serial points at a recorded file (replay mode).

Typical use on the headset:

    python3 fcam_bridge.py --serial /dev/ttyACM0 --listen 0.0.0.0:8555

Then enter fcam://<headset-ip>:8555 as the face camera address in Baballonia.
"""

import argparse
import collections
import logging
import os
import select
import signal
import socket
import stat
import struct
import sys
import threading
import time

MAGIC = b"FCAM"
VERSION = 1
HEADER = struct.Struct(">4sBBBBHHHHIII")
HEADER_LEN = HEADER.size  # 28

TYPE_FRAME = 1
TYPE_STATUS = 2
TYPE_SUBSCRIBE = 3
TYPE_UNSUBSCRIBE = 4

CODEC_NONE = 0
CODEC_JPEG = 1

FLAG_SOURCE_PRESENT = 0x01

DEFAULT_PORT = 8555
DEFAULT_CHUNK = 1400  # payload bytes per datagram; 28 B header + 1400 B stays under a 1500 B MTU
MAX_CHUNK = 65535 - HEADER_LEN

# OpenIris / Babble serial framing: header, u16 little-endian length, JPEG bytes
ETVR_HEADER = b"\xff\xa0\xff\xa1"
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"

# Espressif USB JTAG/serial debug unit: the ESP32-S3 in the Babble tracker
DEFAULT_USB_ID = "303a:1001"

__version__ = "0.2.0"

log = logging.getLogger("fcam")


# ----------------------------------------------------------------------------- protocol helpers

def pack_header(msg_type, codec=CODEC_NONE, flags=0, seq=0, chunk_index=0, chunk_count=0,
                payload_len=0, frame_len=0, chunk_offset=0, timestamp_ms=0):
    return HEADER.pack(MAGIC, VERSION, msg_type, codec, flags, seq & 0xFFFF, chunk_index & 0xFFFF,
                       chunk_count & 0xFFFF, payload_len & 0xFFFF, frame_len & 0xFFFFFFFF,
                       chunk_offset & 0xFFFFFFFF, timestamp_ms & 0xFFFFFFFF)


def unpack_header(data):
    """Return the header fields after magic/version as a tuple, or None if not an FCAM v1 datagram."""
    if len(data) < HEADER_LEN:
        return None
    fields = HEADER.unpack_from(data)
    if fields[0] != MAGIC or fields[1] != VERSION:
        return None
    # type, codec, flags, seq, chunk_index, chunk_count, payload_len, frame_len, chunk_offset, timestamp
    return fields[2:]


def monotonic_ms():
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def parse_hostport(text, default_host="0.0.0.0", default_port=DEFAULT_PORT, allow_zero=False):
    """'host:port', 'host', ':port' or '[v6]:port' -> (host, port). Port 0 (ephemeral) only with allow_zero."""
    text = text.strip()
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError("bad address %r" % text)
        host = text[1:end]
        rest = text[end + 1:]
        port = int(rest[1:]) if rest.startswith(":") else default_port
    elif text.count(":") == 1:
        host, port_text = text.split(":")
        port = int(port_text) if port_text else default_port
    else:  # plain host, or a bare IPv6 literal
        host, port = text, default_port
    if not host:
        host = default_host
    if not (0 if allow_zero else 1) <= port <= 65535:
        raise ValueError("bad port in %r" % text)
    return host, port


# ----------------------------------------------------------------------------- serial framing

class EtvrParser:
    """Splits a byte stream into JPEG frames using the OpenIris/Babble serial framing.

    Anything between frames (firmware log text) is passed to on_text. A frame whose
    payload does not start with the JPEG SOI marker and end with the EOI marker is
    counted as bad and the parser resynchronises on the next header.
    """

    def __init__(self, on_frame, on_text=None):
        self._buf = bytearray()
        self._on_frame = on_frame
        self._on_text = on_text
        self.frames = 0
        self.bad_frames = 0
        self.text_bytes = 0

    def feed(self, data):
        buf = self._buf
        buf += data
        while True:
            start = buf.find(ETVR_HEADER)
            if start < 0:
                # Keep the last 3 bytes: a header may straddle the read boundary.
                cut = max(0, len(buf) - 3)
                self._text(buf[:cut])
                del buf[:cut]
                return
            if start > 0:
                self._text(buf[:start])
                del buf[:start]
            if len(buf) < 6:
                return
            length = buf[4] | (buf[5] << 8)
            if length < 4:
                self.bad_frames += 1
                del buf[:2]
                continue
            if len(buf) < 6 + length:
                return
            frame = bytes(buf[6:6 + length])
            if frame[:2] == JPEG_SOI and frame[-2:] == JPEG_EOI:
                del buf[:6 + length]
                self.frames += 1
                self._on_frame(frame)
            else:
                self.bad_frames += 1
                del buf[:2]

    def _text(self, chunk):
        if not chunk:
            return
        self.text_bytes += len(chunk)
        if self._on_text is not None:
            self._on_text(bytes(chunk))


def split_jpegs(data):
    """Fallback for raw MJPEG files without ETVR headers: split on SOI markers."""
    frames = []
    pos = data.find(JPEG_SOI)
    while pos >= 0:
        nxt = data.find(JPEG_SOI, pos + 2)
        chunk = data[pos:nxt] if nxt >= 0 else data[pos:]
        end = chunk.rfind(JPEG_EOI)
        if end > 0:
            frames.append(bytes(chunk[:end + 2]))
        pos = nxt
    return frames


# ----------------------------------------------------------------------------- device discovery

def find_tracker_ports(usb_id=DEFAULT_USB_ID, usb_serial=None, sysfs="/sys", dev="/dev"):
    """Serial ports whose USB parent matches vendor:product (and serial, if given), found via sysfs.

    Returns dicts (path, node, serial, product) sorted by node name, so the bridge can follow the
    tracker when it re-enumerates as a different /dev/ttyACMn.
    """
    vendor, _, product_id = usb_id.lower().partition(":")
    found = []
    class_tty = os.path.join(sysfs, "class", "tty")
    try:
        nodes = sorted(os.listdir(class_tty))
    except OSError:
        return found
    for node in nodes:
        if not node.startswith(("ttyACM", "ttyUSB")):
            continue
        usb_dev = _usb_parent(os.path.join(class_tty, node, "device"))
        if usb_dev is None:
            continue
        if _sysfs_read(usb_dev, "idVendor").lower() != vendor or _sysfs_read(usb_dev, "idProduct").lower() != product_id:
            continue
        serial = _sysfs_read(usb_dev, "serial")
        if usb_serial and serial.lower() != usb_serial.lower():
            continue
        found.append({"path": os.path.join(dev, node), "node": node, "serial": serial,
                      "product": _sysfs_read(usb_dev, "product")})
    return found


def _usb_parent(device_link):
    """The sysfs directory of the USB device a tty interface belongs to (walks up a few levels)."""
    path = os.path.realpath(device_link)
    for _ in range(4):
        if os.path.exists(os.path.join(path, "idVendor")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return None


def _sysfs_read(directory, name):
    try:
        with open(os.path.join(directory, name), encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


# ----------------------------------------------------------------------------- frame sources

class FrameQueue:
    """Small latest-N buffer between the source thread and the sender loop."""

    def __init__(self, wake_sock, maxlen=8):
        self._dq = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._wake = wake_sock
        self.dropped = 0

    def put(self, frame):
        with self._lock:
            if len(self._dq) == self._dq.maxlen:
                self.dropped += 1
            self._dq.append((frame, monotonic_ms()))
        try:
            self._wake.send(b"x")
        except OSError:
            pass

    def drain(self):
        with self._lock:
            items = list(self._dq)
            self._dq.clear()
        return items


class SerialSource(threading.Thread):
    """Reads the tracker's serial port (or a FIFO) and pushes ETVR frames into the queue.

    With path "auto" the port is looked up through sysfs by USB vendor:product (and serial) on
    every open, so the tracker is found again after it re-enumerates as a different /dev/ttyACMn.
    Reopens on loss.
    """

    def __init__(self, path, baud, frames, stop_event, log_text, usb_id=DEFAULT_USB_ID, usb_serial=None,
                 sysfs="/sys", dev="/dev"):
        super().__init__(name="serial-source", daemon=True)
        self.spec = path
        self.auto = path == "auto"
        self.path = None if self.auto else path
        self.usb_id = usb_id
        self.usb_serial = usb_serial
        self.sysfs = sysfs
        self.dev = dev
        self.device_serial = None  # USB serial of the unit currently (or last) opened
        self.baud = baud
        self.frames = frames
        self.stop = stop_event
        self.log_text = log_text
        self.state = "opening"
        self.parser = EtvrParser(self.frames.put, self._on_text)
        self.opens = 0
        self._waiting_logged = False

    @property
    def description(self):
        if not self.auto:
            return self.spec
        if self.path:
            return "%s (usb %s serial %s)" % (self.path, self.usb_id, self.device_serial or "?")
        return "waiting for usb %s%s" % (self.usb_id, " serial " + self.usb_serial if self.usb_serial else "")

    def _discover(self):
        """Path of the tracker's port, or None. Prefers the unit used last when several match."""
        ports = find_tracker_ports(self.usb_id, self.usb_serial, self.sysfs, self.dev)
        if not ports:
            if not self._waiting_logged:
                log.warning("no serial port with usb id %s%s; waiting for the tracker", self.usb_id,
                            " serial " + self.usb_serial if self.usb_serial else "")
                self._waiting_logged = True
            return None
        self._waiting_logged = False
        choice = next((p for p in ports if p["serial"] == self.device_serial), ports[0])
        if len(ports) > 1:
            log.info("%d ports match usb id %s, using %s", len(ports), self.usb_id, choice["path"])
        if choice["path"] != self.path or choice["serial"] != self.device_serial:
            log.info("tracker %s (serial %s) is at %s", choice["product"] or self.usb_id, choice["serial"] or "?",
                     choice["path"])
        self.device_serial = choice["serial"]
        return choice["path"]

    def run(self):
        while not self.stop.is_set():
            fd = self._open()
            if fd is None:
                self.state = "no-source"
                self.stop.wait(1.0)
                continue
            self.state = "streaming"
            try:
                self._read_loop(fd)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if not self.stop.is_set():
                self.state = "no-source"
                log.warning("%s closed, waiting for it to come back", self.path or self.spec)
                self.stop.wait(1.5)  # a re-enumerating USB device needs a moment before its node is back

    def _open(self):
        path = self._discover() if self.auto else self.spec
        if path is None:
            return None
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            if self.state != "no-source":
                log.warning("cannot open %s: %s", path, e.strerror)
            return None
        try:
            if os.isatty(fd):
                self._configure_tty(fd)
        except Exception as e:  # report and retry later
            log.error("cannot configure %s: %s", path, e)
            os.close(fd)
            return None
        self.path = path
        self.opens += 1
        log.info("opened %s (%s, %d baud)%s", path, "tty" if os.isatty(fd) else "stream", self.baud,
                 " usb serial %s" % self.device_serial if self.auto and self.device_serial else "")
        return fd

    def _configure_tty(self, fd):
        import termios  # only needed for real serial ports (Linux/macOS)
        attrs = termios.tcgetattr(fd)
        speed = getattr(termios, "B%d" % self.baud, None)
        if speed is None:
            raise ValueError("baud rate %d is not supported by this platform" % self.baud)
        attrs[0] = 0  # iflag: no input translation
        attrs[1] = 0  # oflag: no output translation
        # No HUPCL: keep DTR/RTS asserted when we close, so a reopen never toggles the modem lines
        # (an ESP32's USB-Serial/JTAG port can reset the chip on DTR/RTS transitions).
        cflag = attrs[2] & ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.HUPCL)
        cflag |= termios.CS8 | termios.CLOCAL | termios.CREAD
        if hasattr(termios, "CRTSCTS"):
            cflag &= ~termios.CRTSCTS
        attrs[2] = cflag
        attrs[3] = 0  # lflag: raw, no echo
        attrs[4] = speed
        attrs[5] = speed
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)

    def _read_loop(self, fd):
        while not self.stop.is_set():
            try:
                readable, _, _ = select.select([fd], [], [], 0.5)
            except (OSError, ValueError):
                return
            if not readable:
                continue
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                continue
            except OSError as e:
                log.warning("read error on %s: %s", self.path, e.strerror)
                return
            if not data:
                return  # EOF: FIFO writer went away or the device vanished
            self.parser.feed(data)

    def _on_text(self, chunk):
        if not self.log_text:
            return
        text = chunk.decode("utf-8", "replace").strip()
        if text:
            log.debug("tracker: %s", text[:300])


class ReplaySource(threading.Thread):
    """Replays frames from a recorded file (ETVR stream or raw MJPEG) at a fixed rate."""

    def __init__(self, path, fps, frames, stop_event):
        super().__init__(name="replay-source", daemon=True)
        self.path = path
        self.fps = fps
        self.frames = frames
        self.stop = stop_event
        self.state = "opening"
        self._frames = []
        self.parser = EtvrParser(self._frames.append)
        self.opens = 0

    @property
    def description(self):
        return "replay:%s" % self.path

    def run(self):
        with open(self.path, "rb") as fh:
            data = fh.read()
        self.parser.feed(data)
        if not self._frames:
            self._frames = split_jpegs(data)
        if not self._frames:
            log.error("no JPEG frames found in %s", self.path)
            self.state = "no-source"
            return
        self.opens = 1
        log.info("replaying %d frames from %s at %.1f fps", len(self._frames), self.path, self.fps)
        self.state = "streaming"
        interval = 1.0 / self.fps
        next_at = time.monotonic()
        index = 0
        while not self.stop.is_set():
            self.frames.put(self._frames[index])
            index = (index + 1) % len(self._frames)
            next_at += interval
            delay = next_at - time.monotonic()
            if delay > 0:
                self.stop.wait(delay)
            else:
                next_at = time.monotonic()


# ----------------------------------------------------------------------------- bridge

class Bridge:
    def __init__(self, args, source, frames, wake_r, stop_event):
        self.args = args
        self.source = source
        self.frames = frames
        self.wake_r = wake_r
        self.stop = stop_event
        self.chunk = args.chunk
        self.sub_ttl = args.sub_ttl
        self.subs = {}  # (host, port) -> expiry (monotonic seconds); changed by the bridge thread only
        self._subs_lock = threading.Lock()  # guards changes to subs against snapshot() from other threads
        self.targets = [parse_hostport(t) for t in args.target]
        self.seq = 0
        self.sent_frames = 0
        self.unsent_frames = 0
        self.send_errors = 0
        self.started = time.monotonic()
        self._fps = 0.0
        self._fps_window_start = self.started
        self._fps_window_frames = 0

        host, port = parse_hostport(args.listen, allow_zero=True)
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.sock = socket.socket(family, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        self.sock.bind((host, port))
        self.sock.setblocking(False)
        self.listen_addr = tuple(self.sock.getsockname()[:2])  # the real port when 0 was requested

    # -- destinations

    def _destinations(self):
        return list(self.subs.keys()) + self.targets

    def _expire_subscribers(self, now):
        for addr, expiry in list(self.subs.items()):
            if expiry < now:
                with self._subs_lock:
                    del self.subs[addr]
                log.info("subscriber %s:%d timed out", addr[0], addr[1])

    def _subscribe(self, key, name):
        if key not in self.subs:
            log.info("subscriber %s:%d joined (%s)", key[0], key[1], name)
        with self._subs_lock:
            self.subs[key] = time.monotonic() + self.sub_ttl

    def _unsubscribe(self, key):
        with self._subs_lock:
            removed = self.subs.pop(key, None) is not None
        if removed:
            log.info("subscriber %s:%d left", key[0], key[1])

    def _recv_control(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except BlockingIOError:
                return
            except OSError as e:  # e.g. an ICMP unreachable reported on Windows
                log.debug("recv error: %s", e)
                return
            fields = unpack_header(data)
            if fields is None:
                continue
            msg_type = fields[0]
            key = addr[:2]
            if msg_type == TYPE_SUBSCRIBE:
                self._subscribe(key, data[HEADER_LEN:].decode("utf-8", "replace") or "unnamed")
            elif msg_type == TYPE_UNSUBSCRIBE:
                self._unsubscribe(key)

    # -- sending

    def _sendto_all(self, datagram, dests):
        for dest in dests:
            try:
                self.sock.sendto(datagram, dest)
            except OSError as e:
                self.send_errors += 1
                log.debug("send to %s:%d failed: %s", dest[0], dest[1], e)

    def _send_frame(self, frame, timestamp_ms):
        dests = self._destinations()
        if not dests:
            self.unsent_frames += 1
            return
        total = len(frame)
        chunk = self.chunk
        count = max(1, (total + chunk - 1) // chunk)
        seq = self.seq
        for index in range(count):
            offset = index * chunk
            payload = frame[offset:offset + chunk]
            header = pack_header(TYPE_FRAME, CODEC_JPEG, 0, seq, index, count, len(payload), total, offset,
                                 timestamp_ms)
            self._sendto_all(header + payload, dests)
        self.seq = (seq + 1) & 0xFFFF
        self.sent_frames += 1
        self._fps_window_frames += 1

    def _status_text(self, state=None):
        state = state or self.source.state
        return "state=%s;source=%s;fps=%.1f;frames=%d;bad=%d;dropped=%d;subs=%d;uptime=%d" % (
            state, self.source.description, self._fps, self.sent_frames, self.source.parser.bad_frames,
            self.frames.dropped, len(self.subs), int(time.monotonic() - self.started))

    def _send_status(self, state=None):
        dests = self._destinations()
        if not dests:
            return
        state = state or self.source.state
        flags = FLAG_SOURCE_PRESENT if state == "streaming" else 0
        payload = self._status_text(state).encode("utf-8")
        header = pack_header(TYPE_STATUS, CODEC_NONE, flags, self.seq, 0, 0, len(payload), 0, 0, monotonic_ms())
        self._sendto_all(header + payload, dests)

    def _update_fps(self, now):
        elapsed = now - self._fps_window_start
        if elapsed >= 1.0:
            self._fps = self._fps_window_frames / elapsed
            self._fps_window_frames = 0
            self._fps_window_start = now

    def snapshot(self):
        """Current state for status displays. Safe to call from another thread: the containers the
        bridge thread changes are copied under the lock, never iterated while they may change."""
        with self._subs_lock:
            subscribers = list(self.subs)
        return {
            "state": self.source.state,
            "source": self.source.description,
            "fps": self._fps,
            "frames": self.sent_frames,
            "bad": self.source.parser.bad_frames,
            "dropped": self.frames.dropped,
            "unsent": self.unsent_frames,
            "send_errors": self.send_errors,
            "subscribers": ["%s:%d" % a for a in subscribers],
            "targets": ["%s:%d" % t for t in tuple(self.targets)],
            "listen_host": self.listen_addr[0],
            "listen_port": self.listen_addr[1],
            "uptime": int(time.monotonic() - self.started),
            "opens": self.source.opens,
        }

    # -- main loop

    def run(self):
        log.info("FCAM bridge %s listening on udp://%s:%d, source %s, chunk %d bytes%s", __version__,
                 self.listen_addr[0], self.listen_addr[1], self.source.description, self.chunk,
                 (", static targets: " + ", ".join("%s:%d" % t for t in self.targets)) if self.targets else "")
        self.source.start()
        interval = self.args.status_interval
        next_status = time.monotonic() + interval
        next_stats = time.monotonic() + self.args.stats if self.args.stats else None
        while not self.stop.is_set():
            try:
                readable, _, _ = select.select([self.sock, self.wake_r], [], [], 0.25)
            except (OSError, ValueError):
                break
            if self.wake_r in readable:
                try:
                    self.wake_r.recv(4096)
                except OSError:
                    pass
            if self.sock in readable:
                self._recv_control()
            for frame, ts in self.frames.drain():
                self._send_frame(frame, ts)
            now = time.monotonic()
            self._update_fps(now)
            if now >= next_status:
                self._expire_subscribers(now)
                self._send_status()
                next_status = now + interval
            if next_stats is not None and now >= next_stats:
                log.info("%s unsent=%d senderr=%d subscribers=%s", self._status_text(), self.unsent_frames,
                         self.send_errors, ", ".join("%s:%d" % a for a in self.subs) or "-")
                next_stats = now + self.args.stats
        self._send_status("stopping")
        self.sock.close()
        log.info("stopped after %d frames", self.sent_frames)


# ----------------------------------------------------------------------------- runtime wrapper

def build_source(args, frames, stop_event):
    """A ReplaySource for a recorded file, otherwise a SerialSource (tty, FIFO, or absent device)."""
    is_regular_file = False
    try:
        is_regular_file = stat.S_ISREG(os.stat(args.serial).st_mode)
    except OSError:
        pass
    if is_regular_file:
        return ReplaySource(args.serial, args.replay_fps, frames, stop_event)
    return SerialSource(args.serial, args.baud, frames, stop_event, args.log_tracker_text,
                        usb_id=args.usb_id, usb_serial=args.usb_serial)


class BridgeRuntime:
    """Owns one bridge with its source thread and sockets; run() blocks, start() runs it on a thread."""

    def __init__(self, args):
        self.args = args
        self.stop_event = threading.Event()
        self.wake_r, self.wake_w = socket.socketpair()
        self.wake_r.setblocking(False)
        self.frames = FrameQueue(self.wake_w)
        self.source = build_source(args, self.frames, self.stop_event)
        self.bridge = Bridge(args, self.source, self.frames, self.wake_r, self.stop_event)
        self.thread = None

    def run(self):
        try:
            self.bridge.run()
        finally:
            self.stop_event.set()
            for sock in (self.wake_r, self.wake_w):
                try:
                    sock.close()
                except OSError:
                    pass

    def start(self):
        self.thread = threading.Thread(target=self.run, name="fcam-bridge", daemon=True)
        self.thread.start()

    def stop(self, timeout=3.0):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout)

    def snapshot(self):
        return self.bridge.snapshot()


# ----------------------------------------------------------------------------- entry point

def build_parser():
    p = argparse.ArgumentParser(description="Forward a serial Babble tracker to Baballonia over FCAM/UDP.")
    p.add_argument("--serial", default="auto",
                   help="'auto' = find the tracker by USB id via sysfs on every open (default); or a serial "
                        "device, FIFO, or a recorded file to replay")
    p.add_argument("--usb-id", default=DEFAULT_USB_ID, metavar="VID:PID",
                   help="USB vendor:product to look for with --serial auto (default: %s)" % DEFAULT_USB_ID)
    p.add_argument("--usb-serial", default=None, metavar="SERIAL",
                   help="only use the tracker with this USB serial number (when several are plugged in)")
    p.add_argument("--baud", type=int, default=3000000, help="serial baud rate (default: 3000000)")
    p.add_argument("--listen", default="0.0.0.0:%d" % DEFAULT_PORT,
                   help="UDP address to receive SUBSCRIBE datagrams on (default: 0.0.0.0:%d)" % DEFAULT_PORT)
    p.add_argument("--target", action="append", default=[], metavar="HOST[:PORT]",
                   help="also push every frame to this receiver (repeatable; for fcam://:PORT listen-only mode)")
    p.add_argument("--chunk", type=int, default=DEFAULT_CHUNK,
                   help="payload bytes per datagram (default: %d)" % DEFAULT_CHUNK)
    p.add_argument("--sub-ttl", type=float, default=3.0, help="seconds a subscription lives without a keepalive")
    p.add_argument("--status-interval", type=float, default=1.0, help="seconds between STATUS datagrams")
    p.add_argument("--replay-fps", type=float, default=30.0, help="frame rate when --serial is a recorded file")
    p.add_argument("--stats", type=float, default=0.0, metavar="SECONDS",
                   help="log a statistics line every SECONDS (default: off)")
    p.add_argument("--log-tracker-text", action="store_true",
                   help="log firmware text received between frames (needs --verbose)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not 1 <= args.chunk <= MAX_CHUNK:
        sys.exit("--chunk must be between 1 and %d" % MAX_CHUNK)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    runtime = BridgeRuntime(args)

    def request_stop(signum, _frame):
        log.info("signal %d, stopping", signum)
        runtime.stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    runtime.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
