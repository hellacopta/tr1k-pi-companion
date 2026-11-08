"""Background link to the TR-1000's editor protocol (USB CDC serial), for the KIT page.

Thread that keeps a live picture of the TR in a TRState:
  connect -> hello -> catalogue (kits, categories, built-in sounds) -> current kit -> tracks,
  then follows the TR's 03 change notices, keeps the session alive like the app does, and runs
  commands queued by the UI (load a sound onto a layer, change kit).

All bytes come from trserial, whose encoding is tested against captures of Roland's app.
Layer B (index 1 of the sound block, layer byte in op 09) and samples (instrument 1000 + sample
id in op 09) were captured 2026-10-28; Layer B samples are the one inferred case.
"""
import queue
import threading
import time
from collections import deque

from . import trserial as ts

LAYER_B_WRITE = True
TRACKS = 10


class TRState:
    """What the UI shows. Replace-on-update for the big catalogue so snapshots stay cheap."""

    def __init__(self):
        self.status = "looking for the TR"
        self.connected = False
        self.firmware = None
        self.kit = None                       # 0-based (TR screen shows kit + 1)
        self.kit_names = {}                   # index -> name
        self.categories = {}                  # id -> name
        self.sounds = {}                      # id -> (category id, type, name), built-ins (< 1000)
        self.samples = {}                     # id -> (category id, type, name, preset), >= 1000
        self.sample_by_path = {}              # path -> sample id (to name a layer's sample)
        # a/b: sound id per layer (1000 = playing a sample); a_sample/b_sample: that sample's id;
        # route: FX route 0 THROUGH / 1 MASTER / 2 ANALOG; sc: SC depth 0..1000;
        # colour: index into trserial.COLOURS, as set on the TR
        self.tracks = [dict(a=None, b=None, a_sample=None, b_sample=None, name=None, cat=None, route=None, sc=None,
                            colour=None, pan=None, rsend=None, dsend=None)
                       for _ in range(TRACKS)]
        self.busy = None                      # text while a command runs
        self.fx = {}                          # trserial.KIT_FX key -> value, for the loaded kit (FX page)
        # Every 03 change notice the TR sends, with the values read back from that range: the
        # LEARN page binds them to parameter names (2026-11-08). (seq, blk, page, idx, off, end, vals)
        self.notices = deque(maxlen=64)
        self.notice_seq = 0


class TRLink(threading.Thread):
    def __init__(self, lock, state):
        super().__init__(daemon=True)
        self.lock, self.st = lock, state
        self.cmds = queue.Queue()
        self.tr = None

    # -- UI side
    def load_sound(self, track, layer, sound_id):
        self.cmds.put(("load", track, layer, sound_id))

    def change_kit(self, kit):
        self.cmds.put(("kit", kit))

    def set_fx(self, key, value):
        """Reverb / delay / side chain (trserial.KIT_FX). Coalesced like the mixer: latest per key."""
        self.cmds.put(("fx", key, value))

    def set_fx_later(self, key, value, delay):
        """A KIT_FX write that waits `delay` seconds (puts a value back after the screen-follow CC)."""
        self.cmds.put(("fx_at", key, value, time.monotonic() + delay))

    def set_mixer(self, track, offset, value):
        """FX route / SC depth. Slider drags queue many; only the latest per track+field is sent."""
        self.cmds.put(("mixer", track, offset, value))

    # -- link side
    def _set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self.st, k, v)

    def _layer_sample(self, kit, sb, layer):
        """Sample id a layer plays: slot from 0f8d (slot + 1), then the slot's path in block 9c."""
        slot = self.tr.read(sb, kit, layer, ts.SAMPLE_SLOT_OFF, 1)
        if not slot or slot[0] == 0:
            return None
        path = self.tr.read(ts.SAMPLE_POOL_BLOCK, slot[0] - 1, 0, ts.SAMPLE_PATH_OFF, ts.SAMPLE_PATH_LEN)
        if not path:
            return None
        if 0 in path:   # NUL-terminated; what follows is padding, then the slot's name at 03bd
            path = path[:path.index(0)]
        text = "".join(chr(v) for v in path if 32 <= v < 127).rstrip()
        with self.lock:
            return self.st.sample_by_path.get(text)

    def _read_track(self, kit, t):
        nb, sb = ts.track_blocks(t)
        a = self.tr.read(sb, kit, 0, ts.SOUND_OFF, 1)
        b = self.tr.read(sb, kit, 1, ts.SOUND_OFF, 1)
        nm = self.tr.read(nb, kit, 0, ts.NAME_OFF, ts.NAME_LEN)
        mx = self.tr.read(ts.MIXER_BLOCK, kit, t, *ts.MIX_WINDOW)
        mx = mx if mx and len(mx) == ts.MIX_WINDOW[1] else None
        a = a[0] if a else None
        b = b[0] if b else None
        row = dict(a=a, b=b,
                   a_sample=self._layer_sample(kit, sb, 0) if a == ts.SAMPLE_PLAYER else None,
                   b_sample=self._layer_sample(kit, sb, 1) if b == ts.SAMPLE_PLAYER else None,
                   name=ts.chars(nm[:16]).rstrip() if nm else None,
                   cat=ts.chars(nm[16:24]).rstrip() if nm else None,
                   pan=mx[0] if mx else None, rsend=mx[1] if mx else None, dsend=mx[2] if mx else None,
                   route=mx[3] if mx else None, sc=mx[4] if mx else None, colour=mx[6] if mx else None)
        with self.lock:
            self.st.tracks[t] = row

    def _read_kit(self):
        kit = self.tr.read(0x03, 0x00, 0, 0x0072, 1)
        if not kit:
            return None
        kit = kit[0]
        self._set(kit=kit)
        for t in range(TRACKS):
            self._read_track(kit, t)
        self._read_fx(kit)
        return kit

    def _read_fx(self, kit):
        fx = {}
        for blk, (first, cnt) in ts.FX_BLOCKS.items():
            vals = self.tr.read(blk, kit, 0, first, cnt)
            for key, (b, off, lo, hi) in ts.KIT_FX.items():
                if b == blk:
                    fx[key] = vals[off - first] if vals and 0 <= off - first < len(vals) else None
        with self.lock:
            self.st.fx = fx

    def _catalogue(self):
        self._set(status="reading kit names")
        _, kits = self.tr.request_list("kits")
        self._set(status="reading categories")
        _, cats = self.tr.request_list("categories")
        self._set(status="reading the sound list")
        _, snd = self.tr.request_list("sounds", settle=1.5)
        sounds, samples, by_path = {}, {}, {}
        for p in snd:
            e = ts.parse_sound_entry(p)
            if e:
                sounds[e[0]] = (e[1], e[2], e[3])
                continue
            e = ts.parse_sample_entry(p)
            if e and e[0] >= ts.SAMPLE_PLAYER:
                samples[e[0]] = (e[1], e[2], e[3], e[5])
                by_path[e[4]] = e[0]
        self._set(kit_names=dict(ts.parse_named_entry(p) for p in kits),
                  categories=dict(ts.parse_named_entry(p) for p in cats),
                  sounds=sounds, samples=samples, sample_by_path=by_path)

    def _run_cmd(self, cmd):
        with self.lock:
            kit = self.st.kit
        if cmd[0] == "load" and kit is not None:
            _, track, layer, sid = cmd
            if layer == 1 and not LAYER_B_WRITE:
                return
            with self.lock:
                if sid >= ts.SAMPLE_PLAYER:
                    cat_id, _, name, _ = self.st.samples.get(sid, (0, 0, "?", True))
                elif sid == 0:
                    cat_id, name = 0, "OFF"
                else:
                    cat_id, _, name = self.st.sounds.get(sid, (0, 0, "?"))
                cat = self.st.categories.get(cat_id, "")
            self._set(busy=f"loading {name}")
            frames = ts.load_sound_frames(track, kit, sid, name, cat, layer)
            self.tr.send(frames[0])           # op 09; the TR pauses while it loads the sound
            self._drain(0.2)
            if frames[1:]:
                self.tr.send(b"".join(frames[1:]))  # one batch (cdc_acm has 16 TX buffers)
            self._drain(0.3)
            self._read_track(kit, track)
            self._set(busy=None)
        elif cmd[0] == "fx" and kit is not None:
            _, key, value = cmd
            self.tr.send(ts.fx_write_frame(key, kit, value))
            with self.lock:   # optimistic; the TR's notice re-reads the block to confirm
                self.st.fx[key] = value
        elif cmd[0] == "mixer" and kit is not None:
            _, track, offset, value = cmd
            self.tr.send(ts.mixer_write_frame(track, kit, offset, value))
            with self.lock:   # optimistic: the TR's notice triggers a re-read to confirm
                self.st.tracks[track][ts.MIX_FIELDS.get(offset, "_other")] = value
        elif cmd[0] == "kit":
            self._set(busy=f"loading kit {cmd[1] + 1}")
            self.tr.send(b"".join(ts.kit_change_frames(cmd[1])))
            self._drain(0.5)
            self._read_kit()
            self._set(busy=None)

    def _read_notices(self):
        """Read back the range each change notice names, for the LEARN page and the log. A notice
        carries start and end offsets; some say page 0x80, which the TR will not read back, so
        those fall back to the current kit."""
        while self._learn:
            blk, page, idx, off, end = self._learn.pop(0)
            cnt = max(1, min(32, end - off + 1)) if end >= off else 1
            vals = self.tr.read(blk, page, idx, off, cnt, timeout=0.5)
            if vals is None and page >= 0x80:
                with self.lock:
                    kit = self.st.kit
                if kit is not None:
                    vals = self.tr.read(blk, kit, idx, off, cnt, timeout=0.5)
            with self.lock:
                self.st.notice_seq += 1
                self.st.notices.append((self.st.notice_seq, blk, page, idx, off, end, vals))
            print(f"[learn] blk {blk:02x} page {page} idx {idx} off {off:04x}..{end:04x} = {vals}", flush=True)

    def _drain(self, seconds):
        """Read for a while, acting on change notices."""
        for f in self.tr.frames(seconds):
            self._on_frame(f)

    def _on_frame(self, f):
        p = ts.payload(f)
        if f[0] != 0x15 or p[0] != 0x03:
            return
        blk = p[1]
        if len(p) >= 11:
            self._learn.append((blk, int.from_bytes(p[3:5], "little"), int.from_bytes(p[5:7], "little"),
                                int.from_bytes(p[7:9], "little"), int.from_bytes(p[9:11], "little")))
        if blk == 0x03 and p[7:9] == (0x0072).to_bytes(2, "little"):
            self._pending_kit = True              # kit changed on the TR (or a pattern pulled one in)
        elif 0x10 <= blk < 0x10 + 10 * TRACKS:
            self._pending_tracks.add((blk - 0x10) // 10)
        elif blk == ts.MIXER_BLOCK:
            t = int.from_bytes(p[5:7], "little")
            if t < TRACKS:
                self._pending_tracks.add(t)
        elif blk in ts.FX_BLOCKS:
            self._pending_fx = True

    def _session(self, path):
        self.tr = ts.TRSerial(path)
        self._pending_kit, self._pending_tracks = False, set()
        self._pending_fx = False
        self._learn = []
        self._later = []
        try:
            self._set(status="connecting")
            fw = self.tr.hello()
            if not fw:
                raise IOError("no hello reply")
            self._set(firmware=fw, status="connected")
            self._catalogue()
            self._read_kit()
            self._set(connected=True, status="connected")
            last_keepalive = last_kit_poll = time.monotonic()
            while True:
                batch = []
                while True:
                    try:
                        batch.append(self.cmds.get_nowait())
                    except queue.Empty:
                        break
                latest = {}
                for cmd in batch:
                    if cmd[0] == "mixer":
                        latest[(cmd[1], cmd[2])] = cmd
                    elif cmd[0] == "fx":
                        latest[("fx", cmd[1])] = cmd
                    elif cmd[0] == "fx_at":
                        self._later.append(cmd)
                    else:
                        self._run_cmd(cmd)
                for cmd in latest.values():
                    self._run_cmd(cmd)
                due = [c for c in self._later if c[3] <= time.monotonic()]
                self._later = [c for c in self._later if c not in due]
                for c in due:
                    self._run_cmd(("fx", c[1], c[2]))
                self._drain(0.1)
                self._read_notices()
                now = time.monotonic()
                if now - last_keepalive >= 1.5:
                    last_keepalive = now
                    for k in ts.KEEPALIVE:
                        self.tr.send(k)
                if self._pending_kit or now - last_kit_poll >= 5.0:   # the app polls too
                    last_kit_poll = now
                    with self.lock:
                        before = self.st.kit
                    kit = self.tr.read(0x03, 0x00, 0, 0x0072, 1)
                    if kit and (kit[0] != before or self._pending_kit):
                        self._read_kit()
                        self._pending_tracks.clear()
                    self._pending_kit = False
                if self._pending_tracks:
                    with self.lock:
                        kit = self.st.kit
                    for t in sorted(self._pending_tracks):
                        self._read_track(kit, t)
                    self._pending_tracks.clear()
                if self._pending_fx:
                    self._pending_fx = False
                    with self.lock:
                        kit = self.st.kit
                    if kit is not None:
                        self._read_fx(kit)
        finally:
            self.tr.close()
            self.tr = None

    def run(self):
        while True:
            path = ts.find_tty()
            if not path:
                self._set(connected=False, status="TR serial port not found")
                time.sleep(2)
                continue
            try:
                self._session(path)
            except (OSError, TimeoutError, TypeError, IndexError, ValueError) as e:
                print(f"[trlink] {e!r}", flush=True)
                self._set(connected=False, status=f"link lost: {e.__class__.__name__}")
                time.sleep(2)
