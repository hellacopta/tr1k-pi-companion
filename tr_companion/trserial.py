"""The TR-1000's editor protocol over its USB CDC serial port (hub port 1, 0582:02E5).

Addresses and frame formats are listed in ../CONTROL-MAP.md. Reads
(hello, list requests, parameter reads) were proven on the Pi 2026-10-28 (probe_serial.py).
The only write is load_sound(), a byte-for-byte replay of the app's Layer A sequence.

Frames (byte 0 type, byte 1 channel):
  14/94: fixed 12 bytes            15/95: 16-byte header + payload (u32 LE length at offset 12)
Header bytes 2-3 are 41 f2 from the host, f0 00 from the TR. Bytes 4-11 are not interpreted by
the TR as far as the captures show (the app sends what looks like stale memory); we send zeros.
"""
import glob
import os
import select
import time

HOST = bytes([0x41, 0xF2])
HELLO = bytes.fromhex("94 02 40 f0 fe c0 00 ff 00 10 0e 03")   # the app's first frame, verbatim

# List ids for the 14-frame "request list" command, and the payload op the entries arrive with.
LISTS = {
    "categories": (0x07, 0x08),
    "sounds": (0x05, 0x06),
    "kits": (0x0B, 0x0C),
    "categories2": (0x0F, 0x11),
    "banks": (0x10, 0x12),
    "tones": (0x0D, 0x0E),
}

NAME_OFF, NAME_LEN = 0x024D, 24      # 16 chars sound name + 8 chars category, one char per value
SOUND_OFF = 0x0B6D                    # global sound id


def track_blocks(track):
    """(name block, sound block) of track 0..9 (BD..RC): blocks 10+10*T and 11+10*T."""
    return 0x10 + 10 * track, 0x11 + 10 * track


def find_tty():
    """The TR's CDC tty, or None. Matches the USB product id, not a fixed ttyACM number."""
    for path in glob.glob("/dev/serial/by-id/*"):
        if "TR-1000" in path or "Roland" in path:
            return os.path.realpath(path)
    for dev in glob.glob("/sys/class/tty/ttyACM*"):
        try:
            usb = os.path.realpath(os.path.join(dev, "device", ".."))
            if open(os.path.join(usb, "idVendor")).read().strip() == "0582" and \
               open(os.path.join(usb, "idProduct")).read().strip() == "02e5":
                return "/dev/" + os.path.basename(dev)
        except OSError:
            continue
    return None


def frame15(channel, payload):
    return bytes([0x15, channel]) + HOST + bytes(8) + len(payload).to_bytes(4, "little") + payload


class Deframer:
    """Byte stream -> frames, same rules as tools/decode_serial.py."""

    def __init__(self):
        self.buf = b""
        self.skipped = 0

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= 12:
            t = self.buf[0]
            if t in (0x14, 0x94):
                n = 12
            elif t in (0x15, 0x95):
                if len(self.buf) < 16:
                    break
                n = 16 + int.from_bytes(self.buf[12:16], "little")
            else:
                self.buf = self.buf[1:]
                self.skipped += 1
                continue
            if len(self.buf) < n:
                break
            out.append(self.buf[:n])
            self.buf = self.buf[n:]
        return out


def payload(f):
    return f[16:] if f[0] in (0x15, 0x95) else f[2:]


# Page is a u16 LE in bytes 3-4 (byte 2 is always 0): sample-pool slots above 255 answer only with
# the high byte in byte 4 (probe_slots.py, 2026-10-28). Kits never exceed 255, so captures match.
def read_request(block, page, index, offset, count):
    return bytes([0x82, block, 0]) + page.to_bytes(2, "little") + index.to_bytes(2, "little") + offset.to_bytes(2, "little") + \
        count.to_bytes(2, "little")


def parse_read_reply(p):
    """02-reply payload -> (block, page, index, offset, [values]). Values are u32 LE."""
    block, page = p[1], int.from_bytes(p[3:5], "little")
    index, offset = int.from_bytes(p[5:7], "little"), int.from_bytes(p[7:9], "little")
    count = int.from_bytes(p[9:11], "little")
    # values follow the 11-byte request echo directly: reply length = 11 + 4 * count (captured)
    vals = [int.from_bytes(p[11 + 4 * i:15 + 4 * i], "little") for i in range(count)]
    return block, page, index, offset, vals


def chars(vals):
    return "".join(chr(v) if 32 <= v < 127 else "?" for v in vals)


class TRSerial:
    def __init__(self, path):
        import termios   # Linux only; the protocol helpers above stay testable on Windows
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = 0                                    # iflag: raw
        attrs[1] = 0                                    # oflag: raw
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0                                    # lflag: no echo, no canonical mode
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self.rx = Deframer()
        self.log = []                                   # (time, dir, frame) for the probe report

    def close(self):
        os.close(self.fd)

    def send(self, data, timeout=3.0):
        """Write all of `data`, waiting while the port is busy.

        The cdc_acm driver has 16 transmit buffers (one per write() call) and the TR stops taking
        data for a moment while it loads a sound (after op 09), so a burst of single-frame writes
        hits EAGAIN after ~16 frames (seen 2026-10-28). Wait for POLLOUT instead of failing, and
        let callers batch frames into one write as the app does.
        """
        self.log.append((time.monotonic(), "TX", data))
        view, end = memoryview(data), time.monotonic() + timeout
        while view:
            try:
                n = os.write(self.fd, view)
                view = view[n:]
            except BlockingIOError:
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"TR serial port busy for {timeout}s, {len(view)} bytes unsent")
                select.select([], [self.fd], [], left)

    def frames(self, timeout):
        """Frames that arrive within `timeout` seconds."""
        end, out = time.monotonic() + timeout, []
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return out
            r, _, _ = select.select([self.fd], [], [], left)
            if r:
                try:
                    data = os.read(self.fd, 4096)
                except BlockingIOError:
                    continue
                for f in self.rx.feed(data):
                    self.log.append((time.monotonic(), "RX", f))
                    out.append(f)

    def hello(self, timeout=2.0):
        self.send(HELLO)
        for f in self.frames(timeout):
            if f[0] == 0x95 and f[1] == 0x02 and payload(f)[:1] == b"\x7e":
                p = payload(f)
                return p[-4:].decode("ascii", "replace")   # firmware version, e.g. "1.50"
        return None

    def request_list(self, name, settle=1.0):
        """Ask for one catalogue; returns (announced count, [entry payloads])."""
        list_id, entry_op = LISTS[name]
        self.send(bytes([0x14, 0x08]) + HOST + bytes([0x80 | list_id, 0x7F, 0, 0, 0, 0, 0, 1]))
        count, entries, end = None, [], time.monotonic() + 15
        while time.monotonic() < end:
            got = self.frames(settle)
            for f in got:
                p = payload(f)
                if f[0] == 0x14 and p[2] == list_id:
                    count = int.from_bytes(p[3:5], "little")
                elif f[0] == 0x15 and p[0] == entry_op:
                    entries.append(p)
            if not got or (count is not None and len(entries) >= count):
                break
        return count, entries

    def read(self, block, page, index, offset, count, timeout=1.5):
        req = read_request(block, page, index, offset, count)
        self.send(frame15(0x08, req))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for f in self.frames(0.1):
                p = payload(f)
                if f[0] == 0x15 and p[0] == 0x02 and p[1:11] == req[1:11]:
                    return parse_read_reply(p)[4]
        return None


def write_request(block, page, index, offset, value):
    return bytes([0x01, block, 0]) + page.to_bytes(2, "little") + index.to_bytes(2, "little") + offset.to_bytes(2, "little") +         value.to_bytes(4, "little")


SAMPLE_PLAYER = 1000   # the "instrument" a sample plays through; tracks holding a sample read 1000


def load_request(sound_id, track, layer=0):
    """op 09: 09 <instrument u16> <sample u16> <track> 00 <layer> 00. Captured 2026-10-28:
      SD A <- 19 8X Tom        09 13 00 00 00 01 00 00 00
      BD B <- 805 Deep SH Bass 09 25 03 00 00 00 00 01 00
      BD B <- OFF (0)          09 00 00 00 00 00 00 01 00
      SD A <- sample 3001      09 e8 03 b9 0b 01 00 00 00   (instrument 1000 = sample player)
    Built-in sounds (< 1000) go in the instrument field; samples (>= 1000) in the sample field."""
    inst, sample = (SAMPLE_PLAYER, sound_id) if sound_id >= SAMPLE_PLAYER else (sound_id, 0)
    return bytes([0x09]) + inst.to_bytes(2, "little") + sample.to_bytes(2, "little") + bytes([track, 0, layer, 0])


def name_values(name, category):
    """24 u32 values: name padded to 16, category padded to 8, as the app writes them."""
    text = name[:16].ljust(16) + category[:8].ljust(8)
    return [ord(c) if 32 <= ord(c) < 127 else 0x20 for c in text]


def load_sound_frames(track, kit, sound_id, name, category, layer=0):
    """The app's sequences, as captured:
      built-in, Layer A: op 09, write the id at index 0, write the 24 name/category chars
      built-in, Layer B: op 09, write the id at index 1 (the name belongs to the track: no write)
      sample,   Layer A: op 09 (instrument 1000 + sample id), write the name -- the TR sets the
                         sound field to 1000 and fills its sample table (block 9c) itself
      sample,   Layer B: not captured; *inferred* as op 09 alone (B never writes the name, and the
                         TR does the rest for samples)."""
    nb, sb = track_blocks(track)
    frames = [frame15(0x08, load_request(sound_id, track, layer))]
    if sound_id < SAMPLE_PLAYER:
        frames.append(frame15(0x08, write_request(sb, kit, layer, SOUND_OFF, sound_id)))
    if layer == 0:
        frames += [frame15(0x08, write_request(nb, kit, 0, NAME_OFF + i, v))
                   for i, v in enumerate(name_values(name, category))]
    return frames


# ---- catalogue entries (payloads of the list streams) ---------------------------------------
def parse_sound_entry(p):
    """op 06, 27-byte form: (id, category id, type, name). None for the 266-byte sample form."""
    if len(p) != 27:
        return None
    u = [int.from_bytes(p[i:i + 2], "little") for i in range(1, 11, 2)]
    return u[0], u[1], u[2], p[11:27].decode("latin-1").rstrip()


PRESET_SAMPLE_ROOT = "A:/Roland/TR-1000/SAMPLE/"


def parse_sample_entry(p):
    """op 06, 266-byte form: (id, category id, type, name, path, preset). The name is the file
    name without its extension (the app wrote "Pulse" for .../FX E/Pulse.SMP). Bytes after the
    path's NUL are stale buffer contents and are ignored."""
    if len(p) != 266:
        return None
    u = [int.from_bytes(p[i:i + 2], "little") for i in range(1, 11, 2)]
    path = p[11:].split(b"\0")[0].decode("latin-1")
    base = path.rsplit("/", 1)[-1]
    name = base.rsplit(".", 1)[0] if "." in base else base
    return u[0], u[1], u[2], name, path, path.startswith(PRESET_SAMPLE_ROOT)


def parse_named_entry(p, width=16):
    """ops 08 / 0c / 11 / 12: (index, name)."""
    return int.from_bytes(p[1:3], "little"), p[3:3 + width].split(b"\0")[0].decode("latin-1").rstrip()


# ---- kit change: what the app wrote when a kit was picked (captured twice, values 18 and 65) --
def kit_change_frames(kit):
    return [frame15(0x08, write_request(0x03, 0x00, 0, 0x0072, kit)),
            frame15(0x08, write_request(0x74, 0x00, 0, 0x0406, kit))]


# ---- keepalives, verbatim from the app (sent every ~1.5 s while it is connected) -------------
KEEPALIVE = [HELLO, bytes.fromhex("14 08 40 f0 9b 01 00 00 00 bc 75 05")]


# ---- samples: which sample a layer plays (captured 2026-10-28) ------------------------------
# A layer playing a sample reads SAMPLE_PLAYER (1000) at SOUND_OFF. The TR keeps the sample itself
# in a pool, block 9c, page = slot (seen up to 359, so not 256 slots); the layer's block holds
# slot + 1 at 0f8d
# (Pulse on SD A: slot 4, 0f8d = 5; Voice FX 11 on BD B: slot 7, 0f8d = 8). Slots are handed out
# as needed, not derived from track/layer.
SAMPLE_SLOT_OFF = 0x0F8D
SAMPLE_POOL_BLOCK = 0x9C
SAMPLE_PATH_OFF, SAMPLE_PATH_LEN = 0x02E7, 214   # NUL-padded; the slot's name follows at 03bd


# ---- mixer, per track (block 0d, page = kit, index = track; captured 2026-10-28) -------------
# 0244..024a, read as one window of 7. Pan and the two sends are read from the app's own connect
# reads (pan 500 on every track = centre; 0245/0246 vary per track, 0-1000) and the order of the
# app's parameter table ([pan], [%], [%], route); WHICH send is reverb and which is delay is a
# guess (0245 reverb, 0246 delay) until checked on the TR. 0249 is not used.
MIXER_BLOCK = 0x0D
PAN_OFF = 0x0244             # 0..1000, 500 = centre
RSEND_OFF = 0x0245           # 0..1000 (reverb send -- unverified order)
DSEND_OFF = 0x0246           # 0..1000 (delay send -- unverified order)
MIX_WINDOW = (PAN_OFF, 7)    # pan, rsend, dsend, route, sc, (0249), colour
FX_ROUTE_OFF = 0x0247        # 0 THROUGH, 1 MASTER, 2 ANALOG
FX_ROUTES = ["THROUGH", "MASTER", "ANALOG"]
SC_DEPTH_OFF = 0x0248        # 0..1000 = 0..100.0 %
# Track colour (captured 2026-10-28: BD set to RED, WHITE, CYAN wrote 0, 11, 5). Names and order
# from the app's own colour menu strings.
COLOUR_OFF = 0x024A
COLOURS = ["RED", "ORANGE", "YELLOW", "LIME", "GREEN", "CYAN", "SKYBLUE", "BLUE", "PURPLE", "MAGENTA",
           "PINK", "WHITE"]


MIX_FIELDS = {PAN_OFF: "pan", RSEND_OFF: "rsend", DSEND_OFF: "dsend", FX_ROUTE_OFF: "route", SC_DEPTH_OFF: "sc",
              COLOUR_OFF: "colour"}


def mixer_write_frame(track, kit, offset, value):
    return frame15(0x08, write_request(MIXER_BLOCK, kit, track, offset, value))


# ---- kit FX: reverb, delay, side chain (LEARNED 2026-11-08 from the TR's own change notices,
# captures/learn-20261108.json: one knob at a time on the TR, each landed on exactly one offset).
# Per-kit blocks, page = kit, index 0. The app's section table names them REVERB / DELAY /
# SIDE_CHAIN (blocks 05 / 06 / 0c). Ranges are what the knobs produced at their ends; the lower
# ends marked ~ were not reached in the capture and are inferred.
# key: (block, offset, lo, hi)
KIT_FX = {
    "rev_type":    (0x05, 0x0959, 0, 5),      # REVERB_TYPES
    "rev_route":   (0x05, 0x095A, 0, 2),      # FX_ROUTES
    "rev_level":   (0x05, 0x095B, 0, 1000),
    "rev_time":    (0x05, 0x095C, 0, 1000),
    "rev_sc":      (0x05, 0x095D, 0, 1000),   # SC depth
    "rev_predly":  (0x05, 0x096F, 0, 100),    # ~lo (6 seen)
    "rev_locut":   (0x05, 0x0970, 0, 16),     # LOW_CUTS
    "rev_hicut":   (0x05, 0x0971, 0, 14),     # HIGH_CUTS (~lo: 1 seen)
    "rev_density": (0x05, 0x0972, 1, 10),
    "dly_type":    (0x06, 0x0980, 0, 3),      # DELAY_TYPES
    "dly_sync":    (0x06, 0x0981, 0, 1),
    "dly_level":   (0x06, 0x0982, 0, 1000),   # ~lo
    "dly_rvb":     (0x06, 0x0983, 0, 1000),   # reverb send, ~lo
    "dly_route":   (0x06, 0x0984, 0, 2),      # FX_ROUTES
    "dly_sc":      (0x06, 0x0987, 0, 1000),
    "dly_time":    (0x06, 0x099D, 0, 1000),   # ~lo (10 seen)
    "dly_fb":      (0x06, 0x099F, 0, 1000),   # ~lo (11 seen), 999 at the top
    "sc_source":   (0x0C, 0x023A, 0, 18),     # SC_SOURCES (1..16 seen; the app's list has 19)
    "sc_modetime": (0x0C, 0x023B, 0, 500),
    "sc_release":  (0x0C, 0x023C, 0, 1000),   # ~lo (1 seen)
    "sc_curve":    (0x0C, 0x023D, 0, 1000),   # ~lo (243 seen)
    "sc_gateduck": (0x0C, 0x023E, 0, 1),      # SC_GATEDUCK
    "sc_mstr":     (0x0C, 0x023F, 0, 1000),   # master depth
    "sc_mode":     (0x0C, 0x0241, 0, 1),      # SC_MODES
}
# What to read per block to cover every key above: block -> (first offset, count)
FX_BLOCKS = {0x05: (0x0959, 26), 0x06: (0x0980, 32), 0x0C: (0x023A, 8)}
# Option names, from the app's own string tables (TR-1000 App.exe, 2026-11-08). The reverb and
# delay type tables are in the app's section records; the cut lists are the standard Roland
# LOW CUT / HIGH CUT steps whose lengths match the observed ranges (17 and 15) -- inferred.
REVERB_TYPES = ["AMB", "ROOM", "HALL1", "HALL2", "PLATE", "MOD"]
DELAY_TYPES = ["DIGITAL", "PANNING", "TAPE ECHO", "PITCH SHIFT"]
LOW_CUTS = ["FLAT", "20Hz", "25Hz", "31.5Hz", "40Hz", "50Hz", "63Hz", "80Hz", "100Hz", "125Hz", "160Hz", "200Hz",
            "250Hz", "315Hz", "400Hz", "500Hz", "630Hz"]
HIGH_CUTS = ["630Hz", "800Hz", "1kHz", "1.25kHz", "1.6kHz", "2kHz", "2.5kHz", "3.15kHz", "4kHz", "5kHz", "6.3kHz",
             "8kHz", "10kHz", "12.5kHz", "FLAT"]
# Side chain option lists, from the app's parameter table (2026-11-08): they sit in order next to
# [env_hold_time] 0-500 (MODE TIME), [env_decay] 0-1000 (RELEASE) and [crv] 0-1000 (CURVE), which
# match the learned ranges. The source list replaces an earlier guess (OFF, BD..RC, EXT IN), which
# was a different parameter's list.
SC_SOURCES = ["OFF", "BD A&B", "BD A", "BD B", "SD A&B", "SD A", "SD B", "LT A&B", "LT A", "LT B",
              "HT A&B", "HT A", "HT B", "RS", "HC", "CH", "OH", "CC", "RC"]
SC_GATEDUCK = ["DUCK", "GATE"]
SC_MODES = ["TIME", "STEP"]


def fx_write_frame(key, kit, value):
    blk, off, lo, hi = KIT_FX[key]
    return frame15(0x08, write_request(blk, kit, 0, off, max(lo, min(hi, int(value)))))
