# FCAM: face camera stream over UDP

FCAM is a small datagram protocol that carries JPEG frames from a face tracker
that is physically attached to one machine (for example a Babble tracker on the
Steam Frame's USB-C port, visible there as `/dev/ttyACM0`) to Baballonia running
on another machine. It was designed for the Steam Frame, where the stock kernel
has no USB video or serial class drivers and the only reliable link to the PC is
Wi-Fi.

Design goals:

- **Push, not pull.** UDP datagrams with no connection to keep alive. A lost
  datagram costs one frame, never a stall, which is what a tracker wants.
- **Works through the PC firewall without rules.** Baballonia sends a
  `SUBSCRIBE` datagram first, so the bridge's frames are replies to an outbound
  datagram and Windows Defender Firewall lets them in.
- **Nothing beyond the Python standard library on the sender.** The bridge runs
  on SteamOS with Python 3.12 and no pip packages.
- **Self-describing chunks.** Every datagram carries the frame length and its
  own byte offset, so the receiver can reassemble frames out of order and drop
  incomplete ones without extra state.

Transport: UDP. Default port **8555**. Byte order: big-endian (network order).

## Datagram header (28 bytes)

Every datagram, in both directions, starts with this header.

| Offset | Size | Field          | Description |
|-------:|-----:|----------------|-------------|
| 0      | 4    | `magic`        | ASCII `FCAM` |
| 4      | 1    | `version`      | `1` |
| 5      | 1    | `type`         | `1` FRAME, `2` STATUS, `3` SUBSCRIBE, `4` UNSUBSCRIBE |
| 6      | 1    | `codec`        | `1` JPEG for FRAME, `0` otherwise |
| 7      | 1    | `flags`        | STATUS: bit 0 = a source device is present. Otherwise 0. |
| 8      | 2    | `frame_seq`    | Frame counter, wraps at 65536. All chunks of one frame share it. |
| 10     | 2    | `chunk_index`  | 0-based index of this chunk within the frame |
| 12     | 2    | `chunk_count`  | Number of chunks in the frame (at least 1 for FRAME) |
| 14     | 2    | `payload_len`  | Number of payload bytes that follow the header |
| 16     | 4    | `frame_len`    | Total length of the frame in bytes |
| 20     | 4    | `chunk_offset` | Byte offset of this chunk's payload inside the frame |
| 24     | 4    | `timestamp_ms` | Sender monotonic clock in milliseconds, wraps at 2^32 |

Python `struct` format: `>4sBBBBHHHHIII`. C#: see `FcamHeader` in
`src/Baballonia.FcamStreamCapture/FcamProtocol.cs` of [Baballonia](https://github.com/Project-Babble/Baballonia).

A receiver ignores any datagram whose magic or version does not match, whose
`payload_len` does not equal the number of bytes after the header, or whose
`type` it does not know.

## Message types

### FRAME (bridge → receiver)

One JPEG frame split into `chunk_count` datagrams. The bridge uses a fixed
payload size (default 1400 bytes, so a datagram stays under a 1500-byte MTU)
for every chunk but the last. The receiver:

1. starts a new frame when it sees a `frame_seq` it is not currently assembling;
   a frame still incomplete at that point is dropped;
2. copies each payload to `chunk_offset`, ignoring duplicates and chunks whose
   offset or length exceeds `frame_len`;
3. decodes the frame once all `chunk_count` chunks arrived, and validates the
   JPEG by decoding it (the bridge already checked SOI/EOI markers).

Chunks of a `frame_seq` older than the one being assembled (in modular
arithmetic) are ignored.

### STATUS (bridge → receiver)

Sent once per second to every subscriber and static target. `payload` is UTF-8
text of `key=value` pairs separated by `;`, for example:

```
state=streaming;source=/dev/ttyACM0;fps=44.8;frames=12034;bad=0;dropped=0;subs=1;uptime=812
```

`state` is one of `streaming`, `opening`, `no-source`, `stopping`. Receivers
should show the state to the user but must not rely on any particular key.
`flags` bit 0 mirrors `state=streaming`.

### SUBSCRIBE (receiver → bridge)

Sent by the receiver once per second while it wants frames. The bridge records
the datagram's source address and streams to it until 3 seconds pass without a
new SUBSCRIBE. `payload` may carry a UTF-8 client name for logs. The bridge
must answer from the same socket it received the SUBSCRIBE on, so that
stateful firewalls on the receiver side accept the frames as replies.

### UNSUBSCRIBE (receiver → bridge)

Best-effort notice that the receiver is stopping. The bridge removes the
subscriber immediately instead of waiting for the timeout.

## Addresses in Baballonia

The `Baballonia.FcamStreamCapture` module claims camera addresses that start
with `fcam://`:

| Address                    | Behaviour |
|----------------------------|-----------|
| `fcam://172.16.0.249:8555` | Subscribe mode. Baballonia binds an ephemeral UDP port, sends SUBSCRIBE to the bridge every second and receives frames on that port. |
| `fcam://172.16.0.249`      | Same, default port 8555. |
| `fcam://otter-frame:8555`  | Host names are resolved when capture starts. |
| `fcam://:8555`             | Listen-only mode on UDP 8555. Use with `fcam_bridge.py --target <pc-ip>:8555` on the sender. Needs an inbound firewall rule on the PC. |

## Serial source framing (tracker → bridge)

The Babble tracker firmware (OpenIris) writes frames to its USB CDC-ACM port as:

```
FF A0 FF A1  <u16 little-endian length>  <JPEG bytes: FF D8 ... FF D9>
```

Firmware log lines are written to the same port between frames. The bridge
locates the 4-byte header, reads the length, and accepts the frame only if the
payload begins with the JPEG SOI marker and ends with the EOI marker; otherwise
it resynchronises on the next header. Scanning for `FF D9` alone is not safe:
the ESP32 encoder's JPEG tables can contain that byte pair inside a frame.

## Bandwidth

A 240x240 tracker frame is 2 to 8 KB, so 45 fps is 0.1 to 0.4 MB/s on the
wire (2 to 6 datagrams per frame). Added latency is roughly one frame time.
