"""TR-1000 companion for the Raspberry Pi touchscreen.

Threads:
  MidiWorker -- reads the TR's raw ALSA MIDI port, follows its clock, runs the LFO/lane engine on
    every clock tick and writes CCs back. All timing-sensitive work lives here so redraws never
    delay a step.
  main thread -- pygame UI (fullscreen, touch arrives as mouse events), presets, settings.
Shared state is guarded by one lock; the port is written outside it.

Laid out on a 1024x600 design
grid that is scaled to whatever the screen reports.
"""
import copy
import json
import os
import queue
import select
import sys
import threading
import time
from collections import deque
from functools import lru_cache

import pygame

from . import automation as au
from . import trmap
from .clock import Clock
from .midi import Parser, TRPort, data_len, find_tr_rawmidi
from .trlink import LAYER_B_WRITE, TRLink, TRState
from . import trserial as ts

W, H = 1024, 600
CONF_DIR = os.path.expanduser("~/.config/tr-companion")
CONF_FILE = os.path.join(CONF_DIR, "presets.json")

def _hex(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


# Roland 808 palette: orange #E8622A, red #D8342A, yellow #F0B828, white #F0EEE8, black #1A1A1C.
# The rest are warm greys between black and white. "cyan" keeps its role as the automation colour
# (LFO/lane-driven values) and is the 808 yellow now; green stays for PLAY and enabled states.
C = dict(bg=_hex("#1A1A1C"), panel=(40, 39, 41), text=_hex("#F0EEE8"), dim=(140, 136, 128),
         green=(120, 200, 96), yellow=_hex("#F0B828"), red=_hex("#D8342A"), cyan=_hex("#F0B828"),
         grid=(62, 60, 62), accent=_hex("#E8622A"), dkred=(88, 26, 22), white=_hex("#F0EEE8"),
         orange=(240, 140, 90), bluedim=(50, 47, 46), dkorange=(104, 46, 22), black=_hex("#1A1A1C"))

# Track colours as the TR shows them (trserial.COLOURS order); RED/ORANGE/YELLOW/WHITE are the 808 ones.
TRACK_RGB = [_hex(h) for h in ("#D8342A", "#E8622A", "#F0B828", "#A8D828", "#3CB44B", "#28C8D8",
                                "#5AA0F0", "#3050E0", "#8A4AE0", "#D838B0", "#F08AB4", "#F0EEE8")]
for _i, _c in enumerate(TRACK_RGB):
    C[f"tc{_i}"] = _c


def light(rgb):
    """True when black text reads better than white on this fill (yellow, white, cyan...)."""
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2] > 150

PAGES = ["KIT", "FX", "LFO", "LANES", "MACRO", "SETUP", "LEARN"]
LEARN_FILE = os.path.join(CONF_DIR, "learn.json")

# LEARN page: the FX parameters to find in the TR's editor protocol (the user's list, 2026-11-08).
# Tap one, change it on the TR, and the change notices that arrive are bound to it (block / index /
# offset / values). The result, learn.json, is what the new FX page will be built from.
LEARN_GROUPS = [
    ("REVERB", ["LEVEL", "TIME", "PRE DLY", "LOW CUT", "HIGH CUT", "TYPE", "DENSITY", "SC DEPTH", "FX ROUTE"]),
    ("DELAY", ["LEVEL", "TIME", "FEEDBACK", "SYNC", "TYPE", "SC DEPTH", "RVB SEND", "FX ROUTE"]),
    ("SIDE CHAIN", ["SOURCE", "CURVE", "MODE", "MODE TIME", "RELEASE", "GATE/DUCK", "MSTR DEP"]),
]

# Sound picker lists, in design units: (x, width, columns) of the category list and the sound
# list. Both scroll by dragging anywhere in them; a tap picks -- except in the right SP_DEAD of
# each column, which only scrolls (the user's request 2026-11-08, replacing the 2026-11-05 scroll
# rails: a thumb resting there while scrolling never picks, a tap further left does).
SP_Y, SP_H, SP_ROW = 100, 384, 48
SP_LISTS = ((8, 230, 1), (250, 768, 3))
SP_DEAD = 0.30       # right fraction of every column that does not pick
SP_TAP_SLOP = 10     # a touch that moves further than this is a scroll, not a tap

# FX page (rebuilt 2026-11-08 on the serial protocol): reverb, delay and side chain with the full
# parameter set from trserial.KIT_FX, plus MASTER for what only MIDI has. The CC versions of
# delay/reverb level/time/feedback stay available as LFO/lane targets (the picker below); the
# serial ones here are 0-1000 and not automatable.
FX_PAGES = [
    # Level / time (/ feedback) are left to the TR's own knobs (2026-11-08); still read, for the
    # screen-follow trick below.
    ("REVERB", [("TYPE", "rev_type"), ("PRE DLY", "rev_predly"), ("LOW CUT", "rev_locut"), ("HIGH CUT", "rev_hicut"),
                ("DENSITY", "rev_density"), ("SC DEPTH", "rev_sc"), ("FX ROUTE", "rev_route")]),
    ("DELAY", [("TYPE", "dly_type"), ("SYNC", "dly_sync"), ("SC DEPTH", "dly_sc"), ("RVB SEND", "dly_rvb"),
               ("FX ROUTE", "dly_route")]),
    ("SIDE CHAIN", [("SOURCE", "sc_source"), ("CURVE", "sc_curve"), ("MODE", "sc_mode"), ("MODE TIME", "sc_modetime"),
                    ("RELEASE", "sc_release"), ("GATE/DUCK", "sc_gateduck"), ("MSTR DEP", "sc_mstr")]),
    ("MASTER", None),   # the MIDI-only ones: filter, drive, analog FX, master FX, morph, ext in
]
# Option names per choice parameter (side chain lists from the app's parameter table).
FX_CHOICES = {"rev_type": ts.REVERB_TYPES, "dly_type": ts.DELAY_TYPES, "rev_locut": ts.LOW_CUTS,
              "rev_hicut": ts.HIGH_CUTS, "sc_source": ts.SC_SOURCES, "rev_route": ts.FX_ROUTES,
              "dly_route": ts.FX_ROUTES, "dly_sync": ["OFF", "ON"], "sc_gateduck": ts.SC_GATEDUCK,
              "sc_mode": ts.SC_MODES}
# TR screen follows the FX tab (2026-11-08, EXPERIMENTAL): the TR shows whichever parameter it
# last received a CC for, so picking REVERB sends the reverb-level CC and DELAY the delay-level
# CC, at the current level. CC steps are coarser than the TR's 0-1000, so the exact level is
# written back over serial a moment later. SIDE CHAIN has no CC, so the TR cannot be sent there.
FX_FOLLOW = {0: (91, "rev_level"), 1: (12, "dly_level")}
FOLLOW_RESTORE_S = 0.3
MASTER_CCS = ["FILTER", "DRIVE", "AFX ON", "MFX ON", "MFX C1", "MFX C2", "MFX C3", "MORPH", "EXT IN"]

# FX parameters in the full picker, one column per section of the TR's panel (names from trmap).
FX_GROUPS = [("FILTER", ["FILTER", "DRIVE"]), ("ANALOG FX", ["AFX ON"]),
             ("MASTER FX", ["MFX ON", "MFX C1", "MFX C2", "MFX C3"]), ("DELAY", ["DLY LVL", "DLY TIME", "DLY FB"]),
             ("REVERB", ["REV TIME", "REV LVL"]), ("MORE", ["MORPH", "EXT IN"])]

# How the lane playhead marks the current step (SETUP). DIM goes darker instead of brighter.
PLAYHEAD = {
    "ORANGE": dict(led="accent", outline="accent", width=3, bar="white", cell="accent"),
    "YELLOW": dict(led="cyan", outline="cyan", width=3, bar="cyan", cell="cyan"),
    "DIM": dict(led="dim", outline="dim", width=2, bar="dkorange", cell="grid"),
}


# ---- shared state ------------------------------------------------------------------------------
class Shared:
    def __init__(self):
        self.lock = threading.Lock()
        self.value = [None] * 128          # last known value per CC; the TR has no SysEx dump
        self.source = [""] * 128           # "tr" / "panel" / "auto"
        self.kit = self.pattern = None
        self.clock = Clock()
        self.cfg = au.defaults()
        # MEASURED 2026-10-28: sound CCs travel on Pattern Ch (10); Kit Ch only switches kits.
        self.settings = dict(kit_ch=1, pat_ch=10, follow=False, playhead="ORANGE", knob="floor")
        self.mon = deque(maxlen=16)
        self.rx = self.tx = self.clocks = self.notes = 0
        self.last_rx = 0.0
        self.port_path = None
        self.pending_pattern = None
        # HOLD (transport bar): automation keeps its clock position but sends nothing, so the TR's
        # screen stops jumping to the automated parameter while you edit on the TR (2026-11-08).
        self.hold = False
        self.trs = TRState()               # live TR state over USB serial (trlink.py)


class MidiWorker(threading.Thread):
    def __init__(self, sh, txq):
        super().__init__(daemon=True)
        self.sh, self.txq = sh, txq
        self.engine = au.Engine()
        self.port = None
        self._last_time = 0.0

    def _write(self, msgs):
        if not self.port or not msgs:
            return
        try:
            self.port.write(b"".join(bytes(m[:1 + data_len(m[0])]) for m in msgs))
        except OSError:
            self._drop()

    def _drop(self):
        if self.port:
            self.port.close()
        self.port = None
        with self.sh.lock:
            self.sh.port_path = None

    def on_msg(self, m, now):
        sh, out = self.sh, []
        status, d1, d2 = m
        typ, ch = status & 0xF0, (status & 0x0F) + 1
        with sh.lock:
            sh.rx += 1
            sh.last_rx = now
            s = sh.settings
            if status == 0xF8:
                sh.clocks += 1
                if sh.clock.running:
                    t = sh.clock.ticks            # the tick this F8 marks (0 = first after Start)
                    sh.clock.on_tick(now)
                    for cc, v in ([] if sh.hold else self.engine.on_tick(sh.cfg, t, now, s.get("knob", "floor"))):
                        sh.value[cc], sh.source[cc] = v, "auto"
                        out.append((0xB0 | (s["pat_ch"] - 1), cc, v))
                else:
                    sh.clock.on_tick(now)
            elif status == 0xFA:
                sh.clock.on_start()
                self.engine.reset()
                sh.mon.append(("IN", m))
            elif status == 0xFB:
                sh.clock.on_continue()
                sh.mon.append(("IN", m))
            elif status == 0xFC:
                sh.clock.on_stop()
                sh.mon.append(("IN", m))
            elif status == 0xF2:
                sh.clock.on_song_position(d1 | d2 << 7)
                sh.mon.append(("IN", m))
            elif typ == 0xB0 and ch in (s["pat_ch"], s["kit_ch"]):
                # knob takeover -- but not for the TR repeating back a value we just sent it
                if self.engine.on_incoming_cc(sh.cfg, d1, d2, now, s.get("knob", "floor")):
                    sh.value[d1], sh.source[d1] = d2, "tr"
                    sh.mon.append(("IN", m))
            elif typ == 0xC0:
                if ch == s["pat_ch"]:
                    sh.pattern = d1
                    if s["follow"]:
                        sh.pending_pattern = d1
                if ch == s["kit_ch"]:
                    sh.kit = d1
                sh.mon.append(("IN", m))
            elif typ in (0x80, 0x90):
                sh.notes += 1                      # one per drum hit: counted, not listed
            elif status < 0xF0:
                sh.mon.append(("IN", m))
        if out:
            self._write(out)
            with sh.lock:
                sh.tx += len(out)

    def _time_tick(self, now):
        """TIME-synced LFOs: evaluated off the wall clock, only while the TR plays (as the others)."""
        if now - self._last_time < au.TIME_TICK_S:
            return
        self._last_time = now
        sh, out = self.sh, []
        with sh.lock:
            if not sh.clock.running or sh.hold:
                return
            s = sh.settings
            for cc, v in self.engine.on_time(sh.cfg, now, s.get("knob", "floor")):
                sh.value[cc], sh.source[cc] = v, "auto"
                out.append((0xB0 | (s["pat_ch"] - 1), cc, v))
        if out:
            self._write(out)
            with sh.lock:
                sh.tx += len(out)

    def run(self):
        parser = Parser()
        next_probe = 0.0
        while True:
            now = time.monotonic()
            if not self.port:
                if now >= next_probe:
                    next_probe = now + 1.0
                    path = find_tr_rawmidi()
                    if path:
                        try:
                            self.port = TRPort(path)
                            parser = Parser()
                            with self.sh.lock:
                                self.sh.port_path = path
                            print(f"[midi] opened {path}", flush=True)
                        except OSError as e:
                            print(f"[midi] {path}: {e}", flush=True)
                time.sleep(0.05)
                continue
            try:
                r, _, _ = select.select([self.port.fd], [], [], 0.003)
                data = self.port.read() if r else b""
            except OSError:
                print("[midi] port lost (TR unplugged or switched off)", flush=True)
                self._drop()
                continue
            stamp = time.monotonic()
            for b in data:
                m = parser.feed(b)
                if m:
                    self.on_msg(m, stamp)
            self._time_tick(stamp)
            msgs = []
            while True:
                try:
                    msgs.append(self.txq.get_nowait())
                except queue.Empty:
                    break
            if msgs:
                self._write(msgs)
                with self.sh.lock:
                    self.sh.tx += len(msgs)
                    for mm in msgs:
                        self.sh.mon.append(("OUT", mm))
                        if mm[0] & 0xF0 == 0xB0:   # a panel fader: its echo is not a TR knob either
                            self.engine.note_sent(mm[1], mm[2], stamp)


# ---- presets ---------------------------------------------------------------------------------
def load_store():
    try:
        with open(CONF_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"slots": {}, "settings": {}}


def save_store(store):
    os.makedirs(CONF_DIR, exist_ok=True)
    tmp = CONF_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f)
    os.replace(tmp, CONF_FILE)   # never leave a half-written file behind


# ---- UI --------------------------------------------------------------------------------------
class App:
    DRAG = {"vfader", "hslider", "lfo_depth", "lfo_centre", "lane_step", "mac_fader", "mt_lo", "mt_hi", "sc_depth",
            "fx_slider", "mix_slider"}

    def __init__(self):
        self.sh = Shared()
        self.txq = queue.Queue()
        self.store = load_store()
        self.sh.settings.update(self.store.get("settings", {}))
        self.slot = 0
        self.load_slot(0)
        self.worker = MidiWorker(self.sh, self.txq)
        self.link = TRLink(self.sh.lock, self.sh.trs)
        # while choosing a sound: dict(track, layer, src, cat, off=[category list, sound list scroll
        # offsets], max=[their limits, set by the draw])
        self.sound_pick = None
        self.list_touch = None         # a touch on a picker list: dict(li, pos, off, moved)
        self.learn = self.load_learn()  # "GROUP/NAME" -> {"hits": [{"blk", "idx", "off", "cnt", "vals": [...]}]}
        self.learn_armed = None        # the "GROUP/NAME" waiting for a change notice
        self.learn_seq = 0             # last notice seq the LEARN page has consumed
        self.fx_tab = 0                # FX page: REVERB / DELAY / SIDE CHAIN / MASTER
        self._inst_tap = 0.0           # time of the last instrument-button tap (double tap = colour)
        self._sp_cache = (None, [], [])
        self.page = 0
        self.sel_inst = self.sel_lane = self.sel_macro = 0
        self.pick = None               # ("lfo", i[, inst]) / ("lane", i) / ("macro", m, t)
        self.dropdown = None           # open dropdown: dict(kind, target, x, y, w, options, current)
        self.pick_page = 0             # full parameter picker: 0 = instruments, 1 = FX
        self.zero_prev = {}            # (what, index) -> value before its name was tapped to zero it
        self.hits = []
        self.drag = None
        self.v = None                  # per-frame snapshot

    # -- scaling from the 1024x600 design grid
    def r(self, x, y, w, h):
        return pygame.Rect(round(x * self.sx), round(y * self.sy), max(1, round(w * self.sx)), max(1, round(h * self.sy)))

    @lru_cache(maxsize=16)
    def font(self, size, bold):
        return pygame.font.SysFont("dejavusans", max(8, round(size * self.sy)), bold=bold)

    @lru_cache(maxsize=2048)
    def render(self, s, size, bold, color):
        return self.font(size, bold).render(s, True, color)

    def text(self, s, x, y, size=20, color="text", bold=False, anchor="topleft"):
        surf = self.render(s, size, bold, C[color])
        rect = surf.get_rect(**{anchor: (round(x * self.sx), round(y * self.sy))})
        self.screen.blit(surf, rect)

    def hit(self, rect, kind, a=None, b=None):
        self.hits.append((rect, kind, a, b))

    def button(self, x, y, w, h, label, on=False, kind=None, a=None, b=None, on_color="accent", size=18,
               caret=False, outline=False):
        rr = self.r(x, y, w, h)
        pygame.draw.rect(self.screen, C[on_color] if on else C["panel"], rr, border_radius=8)
        pygame.draw.rect(self.screen, C["white"] if on or outline else C["grid"], rr, 2, border_radius=8)
        tx = x + w / 2 - (8 if caret else 0)
        self.text(label, tx, y + h / 2, size, ("black" if light(C[on_color]) else "white") if on else "text", True,
                  "center")
        if caret:   # dropdown marker, drawn (not a glyph: font coverage of U+25BE varies)
            cx, cy = x + w - 16, y + h / 2
            pygame.draw.polygon(self.screen, C["dim"], [
                (round((cx - 6) * self.sx), round((cy - 3) * self.sy)),
                (round((cx + 6) * self.sx), round((cy - 3) * self.sy)),
                (round(cx * self.sx), round((cy + 4) * self.sy))])
        if kind:
            self.hit(rr, kind, a, b)

    def vfader(self, x, y, w, h, val, kind, a, color="accent"):
        rr = self.r(x, y, w, h)
        pygame.draw.rect(self.screen, C["panel"], rr, border_radius=8)
        pygame.draw.rect(self.screen, C["grid"], rr, 2, border_radius=8)
        if val is not None:
            fh = (h - 6) * val / 127
            pygame.draw.rect(self.screen, C[color], self.r(x + 3, y + h - 3 - fh, w - 6, fh), border_radius=5)
        self.hit(rr, kind, a)

    def hslider(self, x, y, w, h, val, kind, a, b=None, color="accent", label=None):
        rr = self.r(x, y, w, h)
        pygame.draw.rect(self.screen, C["panel"], rr, border_radius=7)
        pygame.draw.rect(self.screen, C["grid"], rr, 2, border_radius=7)
        if val is not None:
            pygame.draw.rect(self.screen, C[color], self.r(x + 3, y + 3, (w - 6) * val / 127, h - 6), border_radius=5)
        if label:
            self.text(label, x + 10, y + h / 2, 13, "white" if val else "dim", True, "midleft")
        self.text(str(val) if val is not None else "?", x + w - 10, y + h / 2, 18,
                  "white" if val is not None else "dim", True, "midright")
        self.hit(rr, kind, a, b)

    def slider_name(self, name, x, y, size, key, hit_rect):
        """A slider's name; tapping it zeroes the slider, tapping again restores it. Drawn in the
        automation yellow while a value is waiting to be restored."""
        waiting = key in self.zero_prev and self.zero_value(key) == 0
        self.text(name, x, y, size, "cyan" if waiting else "text", True, "midleft")
        self.hit(self.r(*hit_rect), "zero", key)

    def zero_value(self, key):
        what, i = key
        if what == "cc":
            return self.v["value"][i]
        if what == "sc":
            return self.v["tr"]["tracks"][i]["sc"]
        return self.v["cfg"]["lfo"][i][what]      # "depth" / "centre"

    def zero_set(self, key, val):
        what, i = key
        if what == "cc":
            self.queue_cc(i, val)
            self.v["value"][i] = val
        elif what == "sc":
            self.link.set_mixer(i, 0x0248, val)
            self.v["tr"]["tracks"][i]["sc"] = val
        else:
            with self.sh.lock:
                self.sh.cfg["lfo"][i][what] = val

    def open_dropdown(self, kind, target, rect, options, current, swatch=False):
        # anchor in design units, so it lines up with the button that opened it
        self.dropdown = dict(kind=kind, target=target, x=rect.x / self.sx, y=rect.bottom / self.sy,
                             top=rect.y / self.sy, w=max(rect.w / self.sx, 170), options=options, current=current,
                             swatch=swatch)

    def draw_dropdown(self):
        d = self.dropdown
        self.hit(self.screen.get_rect(), "dd_close")       # a tap anywhere else closes it
        ih, n = 48, len(d["options"])
        rows = min(n, 10)                                  # longer lists (cut frequencies) wrap into columns
        cols = (n + rows - 1) // rows
        x = min(d["x"], W - 8 - cols * d["w"])
        y = d["y"] + 4
        if y + ih * rows > 548:                            # no room below: open upwards
            y = d["top"] - 4 - ih * rows
        # Room neither way (the 7 LFO rates from a middle row): keep it on screen, over its button.
        y = max(56, min(y, 548 - ih * rows))
        box = self.r(x - 4, y - 4, cols * d["w"] + 8, ih * rows + 8)
        pygame.draw.rect(self.screen, C["bg"], box, border_radius=10)
        pygame.draw.rect(self.screen, C["accent"], box, 2, border_radius=10)
        for k, opt in enumerate(d["options"]):
            bx, by = x + (k // rows) * d["w"], y + (k % rows) * ih
            self.button(bx, by, d["w"] - 4, ih - 4, opt, k == d["current"], "dd_item", k, size=17)
            if d.get("swatch"):            # track colours: a chip of the colour itself
                pygame.draw.rect(self.screen, C[f"tc{k}"], self.r(bx + 10, by + 12, 20, 20), border_radius=4)

    # -- actions
    def queue_cc(self, cc, v):
        v = max(0, min(127, int(v)))
        with self.sh.lock:
            self.sh.value[cc], self.sh.source[cc] = v, "panel"
            ch = self.sh.settings["pat_ch"]
        self.txq.put((0xB0 | (ch - 1), cc, v))

    def load_slot(self, slot):
        c = au.load(self.store["slots"].get(str(slot)))
        if c is None:
            return False
        with self.sh.lock:
            self.sh.cfg = c
        if hasattr(self, "worker"):
            self.worker.engine.invalidate()
        self.slot = slot
        return True

    def save_slot(self, slot):
        with self.sh.lock:
            self.store["slots"][str(slot)] = copy.deepcopy(self.sh.cfg)
        self.slot = slot
        save_store(self.store)

    # -- LEARN: notices -> names, saved as JSON next to the presets
    def load_learn(self):
        try:
            with open(LEARN_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_learn(self):
        os.makedirs(CONF_DIR, exist_ok=True)
        tmp = LEARN_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.learn, f, indent=1)
        os.replace(tmp, LEARN_FILE)

    def learn_poll(self):
        """Bind notices that arrived since the last frame to the armed parameter."""
        new = [n for n in self.v["tr"]["notices"] if n[0] > self.learn_seq]
        if not new:
            return
        self.learn_seq = new[-1][0]
        if not self.learn_armed:
            return
        rec = self.learn.setdefault(self.learn_armed, {"hits": []})
        for _, blk, page, idx, off, end, vals in new:
            hit = next((h for h in rec["hits"] if (h["blk"], h["idx"], h["off"]) == (blk, idx, off)), None)
            if hit is None:
                hit = dict(blk=blk, idx=idx, off=off, cnt=max(1, end - off + 1), vals=[])
                rec["hits"].append(hit)
            if vals is not None:
                hit["vals"] = (hit["vals"] + [vals])[-8:]
        self.save_learn()

    def tr_follow(self, tab):
        """Send the TR's screen to this FX section (see FX_FOLLOW)."""
        if tab not in FX_FOLLOW or not self.v:
            return
        cc, key = FX_FOLLOW[tab]
        exact = self.v["tr"]["fx"].get(key)
        if exact is not None:
            self.queue_cc(cc, round(exact * 127 / 1000))
            self.link.set_fx_later(key, exact, FOLLOW_RESTORE_S)
        elif self.v["value"][cc] is not None:      # no serial link: resend what MIDI last said
            self.queue_cc(cc, self.v["value"][cc])

    def save_settings(self):
        with self.sh.lock:
            self.store["settings"] = dict(self.sh.settings)
        save_store(self.store)

    def snapshot(self):
        sh = self.sh
        now = time.monotonic()
        with sh.lock:
            return dict(value=list(sh.value), source=list(sh.source), kit=sh.kit, pattern=sh.pattern,
                        cfg=copy.deepcopy(sh.cfg), settings=dict(sh.settings), mon=list(sh.mon),
                        running=sh.clock.running, step=sh.clock.step16, bpm=sh.clock.bpm(now),
                        rx=sh.rx, tx=sh.tx, clocks=sh.clocks, notes=sh.notes,
                        live=now - sh.last_rx < 1.0 if sh.last_rx else False, port=sh.port_path, hold=sh.hold,
                        tr=self._tr_snapshot(), floor=dict(self.worker.engine.floor))

    def _tr_snapshot(self):
        t = self.sh.trs   # caller holds the lock; catalogue dicts are replaced, never mutated
        return dict(status=t.status, connected=t.connected, firmware=t.firmware, kit=t.kit,
                    kit_names=t.kit_names, categories=t.categories, sounds=t.sounds, samples=t.samples,
                    tracks=[dict(x) for x in t.tracks], busy=t.busy, notices=list(t.notices), fx=dict(t.fx),
                    notice_seq=t.notice_seq)

    def track_colour(self, t):
        """C key for track t's colour on the TR; the UI accent until the TR has said."""
        c = self.v["tr"]["tracks"][t].get("colour")
        return f"tc{c}" if self.v["tr"]["connected"] and c is not None and 0 <= c < len(TRACK_RGB) else "accent"

    def layer_text(self, trk, layer):
        """(label, category) for a layer: built-in sounds and samples both show 'id  name' and the
        category from the TR's catalogue. A sample whose slot path is not in the catalogue (moved
        or deleted) falls back to the track's name block for Layer A."""
        tr = self.v["tr"]
        if not tr["connected"]:
            return "-- " + tr["status"], None
        sid = trk["ab"[layer]]
        if sid == 1000:
            ssid = trk["a_sample" if layer == 0 else "b_sample"]
            e = tr["samples"].get(ssid) if ssid else None
            if e:
                return f"{ssid}  {e[2]}", tr["categories"].get(e[0])
            return (f"sample: {trk['name']}" if layer == 0 and trk["name"] else "sample"), \
                (trk["cat"] if layer == 0 else None)
        e = tr["sounds"].get(sid) if sid else None
        return self.sound_label(sid), (tr["categories"].get(e[0]) if e else None)

    def sound_label(self, sid):
        tr = self.v["tr"]
        if sid is None:
            return "?"
        if sid == 0:
            return "OFF"
        if sid >= 1000:
            return "sample / layered"
        e = tr["sounds"].get(sid)
        return f"{sid}  {e[2]}" if e else f"#{sid}"

    # -- pages
    def draw_tabs(self):
        w = W / len(PAGES)
        for i, name in enumerate(PAGES):
            on = i == self.page and not self.pick
            rr = self.r(i * w, 0, w - 3, 46)
            pygame.draw.rect(self.screen, C["accent"] if on else C["panel"], rr)
            self.text(name, i * w + w / 2, 23, 22, "white" if on else "text", True, "center")
            self.hit(rr, "tab", i)

    def draw_transport(self):
        v, y = self.v, 552
        pygame.draw.rect(self.screen, C["panel"], self.r(0, y, W, H - y))
        self.text("PLAY" if v["running"] else "STOP", 16, y + 24, 22, "green" if v["running"] else "dim", True, "midleft")
        self.text(f"{v['bpm']:.1f} BPM" if v["bpm"] else "no clock", 110, y + 24, 22,
                  "text" if v["bpm"] else "dim", True, "midleft")
        for i in range(16):
            cur = v["running"] and v["step"] % 16 == i
            pygame.draw.rect(self.screen, C["accent"] if cur else C["grid"] if i % 4 == 0 else C["bluedim"],
                             self.r(300 + i * 24, y + 13, 20, 22))
        trk = v["tr"]["kit"]
        if trk is not None:   # from the TR itself over serial
            name = v["tr"]["kit_names"].get(trk, "")
            self.text(f"KIT {trk + 1} {name}"[:20], 700, y + 24, 18, "text", False, "midleft")
        else:
            kit = v["kit"] + 1 if v["kit"] is not None else "-"
            ptn = v["pattern"] + 1 if v["pattern"] is not None else "-"
            self.text(f"KIT {kit}  PTN {ptn}", 700, y + 24, 18, "text", False, "midleft")
        if not v["port"]:
            self.text("TR NOT CONNECTED", 888, y + 24, 16, "red", True, "midright")
        # HOLD: mute all lanes/LFOs without touching them, e.g. while editing on the TR itself
        self.button(896, y + 4, 80, 40, "HOLD", v["hold"], "hold", on_color="yellow", size=15)
        pygame.draw.circle(self.screen, C["green"] if v["live"] else C["dkred"],
                           (round((W - 20) * self.sx), round((y + 24) * self.sy)), round(9 * self.sy))

    def draw_kit(self):
        """One instrument: its Layer A / B sounds (from the TR over serial), the voice controls the
        TR has no dedicated knob for, the LFOs driving it, and the kit loader."""
        v, tr = self.v, self.v["tr"]
        for i, name in enumerate(trmap.INST):
            x = 12 + i * 100
            col = self.track_colour(i)
            self.button(x, 52, 94, 50, name, self.sel_inst == i, "inst_sel", i, on_color=col)
            lvl = v["value"][trmap.level_cc(i)]
            if lvl is not None and self.sel_inst != i:
                pygame.draw.rect(self.screen, C[col], self.r(x + 8, 94, 78 * lvl / 127, 4))
        inst = self.sel_inst
        trk = tr["tracks"][inst]
        # Left top: the sounds on this track's two layers.
        for layer, (tag, sid) in enumerate((("LAYER A", trk["a"]), ("LAYER B", trk["b"]))):
            y = 110 + layer * 70
            can = tr["connected"] and (layer == 0 or LAYER_B_WRITE) and tr["kit"] is not None
            pygame.draw.rect(self.screen, C["bluedim"], self.r(12, y, 548, 62), border_radius=10)
            pygame.draw.rect(self.screen, C[self.track_colour(inst)], self.r(12, y, 6, 62),
                             border_top_left_radius=10, border_bottom_left_radius=10)
            self.text(tag, 24, y + 18, 14, "dim", True, "midleft")
            label, cat = self.layer_text(trk, layer)
            self.text(label[:30], 24, y + 42, 22, "white" if tr["connected"] else "dim", True, "midleft")
            if cat and tr["connected"]:
                self.text(cat, 400, y + 18, 14, "dim", False, "midright")
            if can:
                self.button(420, y + 9, 132, 44, "CHANGE", False, "layer_pick", layer, caret=True)
            elif layer == 1 and tr["connected"]:
                self.text("change: needs a capture", 552, y + 31, 13, "dim", False, "midright")
        if tr["busy"]:
            self.text(tr["busy"], 24, 252, 15, "yellow", True, "midleft")
        # Mixer, over serial (block 0d, captured 2026-10-28): FX route and sidechain depth.
        self.text("FX ROUTE", 20, 285, 17, "text", True, "midleft")
        for k, name in enumerate(("THROUGH", "MASTER", "ANALOG")):
            self.button(132 + k * 144, 262, 138, 46, name, trk["route"] == k, "fx_route" if tr["connected"] else None,
                        k, size=16)
        self.slider_name("SC DEPTH", 20, 339, 17, ("sc", inst), (12, 316, 116, 46))
        sc = trk["sc"]
        rr = self.r(132, 316, 428, 46)
        pygame.draw.rect(self.screen, C["panel"], rr, border_radius=7)
        pygame.draw.rect(self.screen, C["grid"], rr, 2, border_radius=7)
        if sc is not None:
            pygame.draw.rect(self.screen, C["accent"], self.r(135, 319, 422 * sc / 1000, 40), border_radius=5)
        self.text(f"{sc / 10:.1f} %" if sc is not None else "?", 550, 339, 18, "white" if sc is not None else "dim",
                  True, "midright")
        if tr["connected"]:
            self.hit(rr, "sc_depth", inst)
        # The voice controls without a dedicated knob on the TR (TUNE, DECAY and LEVEL have their
        # own knobs on every channel; MIX made way for SC depth).
        params = [p for p in trmap.params_for_inst(inst) if p[2] not in ("TUNE", "DECAY", "LEVEL", "MIX")]
        inst_ccs = {p[0] for p in trmap.params_for_inst(inst)}
        for k, (cc, _, name, _) in enumerate(params):
            y = 372 + k * 58
            self.slider_name(name, 20, y + 23, 18, ("cc", cc), (12, y, 116, 46))
            self.hslider(132, y, 428, 46, v["value"][cc], "hslider", cc, None,
                         "cyan" if v["source"][cc] == "auto" else "accent")
        # Right: LFOs whose target is one of this instrument's parameters.
        px = 572
        self.text(f"LFOs ON {trmap.INST[inst]}", px + 6, 112, 18, "dim", True)
        rows = [(i, lf) for i, lf in enumerate(v["cfg"]["lfo"]) if lf["cc"] in inst_ccs][:2]
        for j, (i, lf) in enumerate(rows):
            y = 136 + j * 102
            pygame.draw.rect(self.screen, C["bluedim"], self.r(px, y, 436, 96), border_radius=10)
            self.button(px + 6, y + 4, 74, 44, f"LFO{i + 1}", lf["enabled"], "lfo_en", i, on_color="green", size=15)
            self.button(px + 84, y + 4, 128, 44, trmap.BY_CC[lf["cc"]][2], False, "lfo_tgt_inst", i, size=15)
            self.button(px + 216, y + 4, 120, 44, au.SHAPES[lf["shape"]], False, "lfo_shape", i, size=15, caret=True)
            if lf["enabled"] and lf["paused"]:
                self.button(px + 340, y + 4, 90, 44, "ARM", True, "lfo_arm", i, on_color="yellow", size=15)
            else:
                self.button(px + 340, y + 4, 90, 44, au.rate_label(lf), False, "lfo_rate", i, size=13, caret=True)
            self.hslider(px + 6, y + 52, 210, 38, lf["depth"], "lfo_depth", i, label="DEPTH")
            self.hslider(px + 220, y + 52, 210, 38, lf["centre"], "lfo_centre", i, label="CENTRE")
        free = [i for i, lf in enumerate(v["cfg"]["lfo"]) if lf["cc"] is None]
        if len(rows) < 2:
            y = 136 + len(rows) * 102
            if free:
                self.button(px, y, 436, 46, f"+ ADD LFO TO {trmap.INST[inst]}", False, "lfo_add", free[0])
            else:
                self.text("all 4 LFOs are in use (see the LFO page)", px + 6, y + 20, 15, "dim")
        # Per-instrument mixer over serial (block 0d): pan and the reverb / delay sends, 0..1000.
        # (The kit shows in the bottom bar; double-tap an instrument above for its colour.)
        self.text(f"{trmap.INST[inst]} MIX", px + 6, 360, 18, "dim", True, "midleft")
        for k, (label, field, off) in enumerate((("PAN", "pan", ts.PAN_OFF), ("RVB SEND", "rsend", ts.RSEND_OFF),
                                                 ("DLY SEND", "dsend", ts.DSEND_OFF))):
            y, val = 384 + k * 54, trk.get(field)
            self.text(label, px + 6, y + 23, 16, "text", True, "midleft")
            rr = self.r(px + 110, y, 326, 46)
            pygame.draw.rect(self.screen, C["panel"], rr, border_radius=7)
            pygame.draw.rect(self.screen, C["grid"], rr, 2, border_radius=7)
            if val is not None:
                if field == "pan":           # from the centre outwards
                    x0, x1 = sorted((px + 110 + 163, px + 113 + 320 * val / 1000))
                    pygame.draw.rect(self.screen, C["accent"], self.r(x0, y + 3, max(2, x1 - x0), 40), border_radius=4)
                    shown = "C" if val == 500 else f"L{(500 - val) / 10:.0f}" if val < 500 else f"R{(val - 500) / 10:.0f}"
                else:
                    pygame.draw.rect(self.screen, C["accent"], self.r(px + 113, y + 3, 320 * val / 1000, 40),
                                     border_radius=5)
                    shown = f"{val / 10:.1f} %"
            else:
                shown = "?"
            self.text(shown, px + 428, y + 23, 16, "white" if val is not None else "dim", True, "midright")
            if tr["connected"]:
                self.hit(rr, "mix_slider", inst, off)

    # -- sound picker (KIT page > CHANGE)
    def sp_lists(self):
        """(category ids, [(sound id, name)]) for the open picker.

        Sound 0 is the TR's own "OFF" entry, alone in category OTHER (catalogue read 2026-10-28).
        It is left out: Layer B gets OFF at the top of every list instead (it used to show twice,
        and an OFF Layer B opened on OTHER with nothing else in it), and Layer A is not offered it."""
        sp, tr = self.sound_pick, self.v["tr"]
        src = tr[sp["src"]]
        key = (id(src), len(src), sp["src"], sp["cat"], sp["layer"])   # catalogue dicts are replaced, not mutated
        if self._sp_cache[0] != key:
            cats = sorted({e[0] for sid, e in src.items() if sid})
            if sp["src"] == "samples":   # user samples are marked; presets live under A:/Roland/TR-1000/SAMPLE
                items = sorted((sid, e[2] if e[3] else f"{e[2]} (user)") for sid, e in src.items() if e[0] == sp["cat"])
            else:
                items = sorted((sid, e[2]) for sid, e in src.items() if sid and e[0] == sp["cat"])
            if sp["layer"] == 1:
                items = [(0, "OFF")] + items
            self._sp_cache = (key, cats, items)
        return self._sp_cache[1], self._sp_cache[2]

    def sp_current(self):
        """Id of what the picker's layer plays now: a sample's own id, not the sample player's 1000."""
        sp = self.sound_pick
        trk = self.v["tr"]["tracks"][sp["track"]]
        sid = trk["ab"[sp["layer"]]]
        return trk[("a_sample", "b_sample")[sp["layer"]]] if sid == 1000 else sid

    @staticmethod
    def sp_geom(li):
        """(x, columns, column width) of picker list li."""
        x, w, cols = SP_LISTS[li]
        return x, cols, w / cols

    def sp_place(self, layer):
        """(source, category id) of what a layer of the selected track plays; None when it is OFF
        or not known yet."""
        tr = self.v["tr"]
        trk = tr["tracks"][self.sel_inst]
        sid = trk["ab"[layer]]
        if sid == 1000:
            e = tr["samples"].get(trk[("a_sample", "b_sample")[layer]])
            return "samples", (e[0] if e else None)
        e = tr["sounds"].get(sid) if sid else None
        return ("sounds", e[0]) if e else None

    def sp_centre(self):
        """Scroll both lists so the selected category and the current sound sit mid-list."""
        sp = self.sound_pick
        cats, items = self.sp_lists()
        ids = (cats, [sid for sid, _ in items])
        for li, want in enumerate((sp["cat"], self.sp_current())):
            cols = SP_LISTS[li][2]
            limit = max(0, (len(ids[li]) + cols - 1) // cols * SP_ROW - SP_H)
            row = ids[li].index(want) // cols if want in ids[li] else 0
            sp["max"][li] = limit
            sp["off"][li] = max(0, min(limit, row * SP_ROW - (SP_H - SP_ROW) // 2))

    def draw_sound_picker(self):
        """The TR's built-in sounds or its samples, by category, for one layer of one track."""
        sp, tr = self.sound_pick, self.v["tr"]
        pygame.draw.rect(self.screen, C["bg"], self.r(0, 48, W, 502))
        cats, items = self.sp_lists()
        self.text(f"{trmap.INST[sp['track']]}  LAYER {'AB'[sp['layer']]}", 16, 70, 22, "text", True, "midleft")
        self.button(880, 52, 128, 40, "CLOSE", False, "sp_close", on_color="red")
        self.button(560, 52, 150, 40, "SOUNDS", sp["src"] == "sounds", "sp_src", 0, size=16)
        self.button(716, 52, 150, 40, "SAMPLES", sp["src"] == "samples", "sp_src", 1, size=16)
        rows_of = ([(cid, tr["categories"].get(cid, f"cat {cid}")) for cid in cats],
                   [(sid, f"{sid}  {name}" if sid else "OFF") for sid, name in items])
        selected = (sp["cat"], self.sp_current())
        for li, rows in enumerate(rows_of):
            x, cols, cw = self.sp_geom(li)
            nrows = (len(rows) + cols - 1) // cols
            sp["max"][li] = max(0, nrows * SP_ROW - SP_H)
            off = sp["off"][li] = max(0, min(sp["max"][li], sp["off"][li]))
            self.screen.set_clip(self.r(x, SP_Y, SP_LISTS[li][1], SP_H))
            first = int(off // SP_ROW)
            for row in range(first, min(nrows, first + SP_H // SP_ROW + 1)):
                for c in range(cols):
                    k = row * cols + c
                    if k < len(rows):
                        bx, by = x + c * cw, SP_Y + row * SP_ROW - off
                        self.button(bx, by, cw - 6, SP_ROW - 4, rows[k][1], rows[k][0] == selected[li], size=16)
                        # the scroll-only part of the button, shaded so the thumb knows where to rest
                        dz = self.r(bx + (cw - 6) * (1 - SP_DEAD), by + 2, (cw - 6) * SP_DEAD - 2, SP_ROW - 8)
                        shade = pygame.Surface(dz.size, pygame.SRCALPHA)
                        shade.fill((0, 0, 0, 70))
                        self.screen.blit(shade, dz)
            self.screen.set_clip(None)
            self.hit(self.r(x, SP_Y, SP_LISTS[li][1], SP_H), "sp_list", li)
        self.button(8, 492, 112, 44, "UP", False, "sp_jump", 0, -1)
        self.button(126, 492, 112, 44, "DOWN", False, "sp_jump", 0, 1)
        self.button(250, 492, 120, 44, "UP", False, "sp_jump", 1, -1)
        self.button(376, 492, 120, 44, "DOWN", False, "sp_jump", 1, 1)
        n, first = len(items), int(sp["off"][1] // SP_ROW) * SP_LISTS[1][2]
        self.text(f"{min(n, first + 1)}-{min(n, first + 24)} of {n} {sp['src']}", 512, 514, 16, "dim", False,
                  "midleft")
        self.text(f"now: {self.layer_text(tr['tracks'][sp['track']], sp['layer'])[0]}", 1008, 514, 15, "dim", False,
                  "midright")

    def list_tap(self, li, x, y):
        """A tap (no scroll) on picker list li at design position x, y."""
        sp = self.sound_pick
        lx, cols, cw = self.sp_geom(li)
        c = min(cols - 1, int((x - lx) // cw))
        if (x - lx - c * cw) / cw >= 1 - SP_DEAD:
            return                                   # the right part of a column only scrolls
        k = int((y - SP_Y + sp["off"][li]) // SP_ROW) * cols + c
        cats, items = self.sp_lists()
        if li == 0 and 0 <= k < len(cats):
            sp["cat"] = cats[k]
            sp["off"][1] = 0
        elif li == 1 and 0 <= k < len(items):
            self.sound_pick = None
            self.link.load_sound(sp["track"], sp["layer"], items[k][0])

    def draw_fx(self):
        """REVERB / DELAY / SIDE CHAIN over the serial link (full parameter set, 0-1000 values),
        MASTER for the MIDI-only controls."""
        v, tr = self.v, self.v["tr"]
        for i, (name, _) in enumerate(FX_PAGES):
            self.button(16 + i * 250, 52, 244, 44, name, self.fx_tab == i, "fx_tab", i, size=16)
        name, rows = FX_PAGES[self.fx_tab]
        if rows is None:
            fx = [p for p in trmap.PARAMS if p[1] is None and p[2] in MASTER_CCS]
            for k, (cc, _, pname, toggle) in enumerate(fx):
                x, y = 16 + (k % 2) * 504, 108 + (k // 2) * 62
                if toggle:
                    self.text(pname, x + 4, y + 24, 19, "text", True, "midleft")
                    val = v["value"][cc]
                    on = val is not None and val >= 64
                    self.button(x + 140, y, 350, 48, "? (tap = ON)" if val is None else "ON" if on else "OFF", on, "toggle", cc)
                else:
                    self.slider_name(pname, x + 4, y + 24, 19, ("cc", cc), (x, y, 136, 48))
                    self.hslider(x + 140, y, 350, 48, v["value"][cc], "hslider", cc, None,
                                 "cyan" if v["source"][cc] == "auto" else "accent")
            self.text("delay/reverb level, time and feedback over MIDI are still LFO/lane targets (picker)",
                      W - 16, 540, 13, "dim", False, "midright")
            return
        if not tr["connected"]:
            self.text(f"-- {tr['status']}: these are set over the TR's USB serial link", 20, 130, 18, "dim", True)
            return
        for k, (label, key) in enumerate(rows):
            y = 108 + k * 48
            blk, off, lo, hi = ts.KIT_FX[key]
            val = tr["fx"].get(key)
            self.text(label, 20, y + 22, 18, "text", True, "midleft")
            opts = FX_CHOICES.get(key)
            if opts and len(opts) <= 3:   # segmented
                bw = 848 // len(opts)
                for j, o in enumerate(opts):
                    self.button(160 + j * bw, y, bw - 6, 44, o, val == j, "fx_choice", key, j, size=15)
            elif opts:                    # dropdown
                cur = opts[val] if val is not None and 0 <= val < len(opts) else "?" if val is None else str(val)
                self.button(160, y, 320, 44, cur, False, "fx_dd", key, size=16, caret=True)
            else:                         # slider over the parameter's own range
                rr = self.r(160, y, 848, 44)
                pygame.draw.rect(self.screen, C["panel"], rr, border_radius=7)
                pygame.draw.rect(self.screen, C["grid"], rr, 2, border_radius=7)
                if val is not None:
                    frac = max(0.0, min(1.0, (val - lo) / (hi - lo))) if hi > lo else 0
                    pygame.draw.rect(self.screen, C["accent"], self.r(163, y + 3, 842 * frac, 38), border_radius=5)
                shown = "?" if val is None else f"{val / 10:.1f} %" if hi == 1000 else str(val)
                self.text(shown, 1000, y + 22, 17, "white" if val is not None else "dim", True, "midright")
                self.hit(rr, "fx_slider", key)
        if tr["busy"]:
            self.text(tr["busy"], 20, 540, 14, "yellow", True, "midleft")

    def draw_lfo(self):
        v = self.v
        for i, lf in enumerate(v["cfg"]["lfo"]):
            y = 52 + i * 124
            pygame.draw.rect(self.screen, C["bluedim"], self.r(8, y, W - 16, 118), border_radius=10)
            self.button(16, y + 6, 110, 50, f"LFO {i + 1}", lf["enabled"], "lfo_en", i, on_color="green")
            self.button(134, y + 6, 210, 50, trmap.label(lf["cc"]), False, "lfo_tgt", i, size=16)
            self.button(352, y + 6, 124, 50, au.SHAPES[lf["shape"]], False, "lfo_shape", i, caret=True, size=16)
            self.button(484, y + 6, 104, 50, lf.get("sync", "NOTE"), False, "lfo_sync", i, caret=True, size=16)
            self.button(596, y + 6, 128, 50, au.rate_label(lf), False, "lfo_rate", i, caret=True, size=15)
            if lf["enabled"] and lf["paused"]:
                self.button(732, y + 6, 136, 50, "PAUSED: ARM", True, "lfo_arm", i, on_color="yellow", size=14)
            else:
                self.text("off" if not lf["enabled"] else "running" if v["running"] else "waiting for clock",
                          736, y + 31, 15, "dim", False, "midleft")
            cc = lf["cc"]
            if cc is not None and v["value"][cc] is not None:
                pygame.draw.rect(self.screen, C["panel"], self.r(876, y + 12, 128, 38))
                pygame.draw.rect(self.screen, C["cyan"], self.r(878, y + 14, 124 * v["value"][cc] / 127, 34))
            self.slider_name("DEPTH", 20, y + 89, 14, ("depth", i), (12, y + 64, 64, 48))
            self.hslider(80, y + 64, 640, 48, lf["depth"], "lfo_depth", i)
            self.slider_name("CENTRE", 736, y + 89, 13, ("centre", i), (728, y + 64, 66, 48))
            self.hslider(796, y + 64, 208, 48, lf["centre"], "lfo_centre", i)

    def draw_lanes(self):
        v = self.v
        for i, ln in enumerate(v["cfg"]["lane"]):
            self.button(16 + i * 124, 52, 118, 46, f"LANE {i + 1}", self.sel_lane == i, "lane_sel", i,
                        on_color="green" if ln["enabled"] else "accent", outline=ln["enabled"])
        ln = v["cfg"]["lane"][self.sel_lane]
        self.button(520, 52, 90, 46, "ON" if ln["enabled"] else "OFF", ln["enabled"], "lane_en", self.sel_lane, on_color="green")
        self.button(618, 52, 220, 46, trmap.label(ln["cc"]), False, "lane_tgt", self.sel_lane)
        self.button(846, 52, 160, 46, f"{ln['length']} STEPS", False, "lane_len", self.sel_lane)
        if ln["enabled"] and ln["paused"]:
            self.button(520, 102, 486, 30, "PAUSED by the TR knob: tap to ARM", True, "lane_arm", self.sel_lane,
                        on_color="yellow", size=15)
        elif ln["enabled"] and ln["cc"] is not None and v["settings"].get("knob", "floor") == "floor":
            fl = v["floor"].get(ln["cc"], 0)
            self.text(f"TR knob = floor {fl}: steps below it play at {fl}" if fl else "TR knob = floor (at 0: steps play as set)",
                      1006, 117, 14, "yellow" if fl else "dim", False, "midright")
        top, h = 150, 330
        # Playhead: the lane's own position (its length can be shorter than the TR's 16 steps),
        # exactly the step the engine sends on. Only while the TR's clock is running.
        playing = v["step"] % ln["length"] if v["running"] and ln["length"] else -1
        ph = PLAYHEAD.get(v["settings"].get("playhead"), PLAYHEAD["ORANGE"])   # SETUP choice
        for s in range(au.LANE_STEPS):
            x, in_len, val = 16 + s * 62, s < ln["length"], ln["steps"][s]
            if not in_len:
                continue
            cur = s == playing
            # Step light, TR style: playhead colour = now, dim orange = has a value, outline every beat.
            led = self.r(x + 4, 136, 50, 10)
            pygame.draw.rect(self.screen, C[ph["led"]] if cur else C["dkorange"] if val is not None else C["bluedim"],
                             led, border_radius=3)
            if s % 4 == 0 and not cur:
                pygame.draw.rect(self.screen, C["dim"], led, 1, border_radius=3)
            rr = self.r(x, top, 58, h)
            pygame.draw.rect(self.screen, C["grid"] if cur else C["panel"], rr, border_radius=5)
            if val is not None:
                fh = (h - 6) * val / 127
                pygame.draw.rect(self.screen, C[ph["bar"]] if cur else C["accent"], self.r(x + 3, top + h - 3 - fh, 52, fh))
            self.hit(rr, "lane_step", self.sel_lane, s)
            cr = self.r(x, top + h + 8, 58, 40)
            pygame.draw.rect(self.screen, C[ph["cell"]] if cur and val is not None else C["panel"], cr, border_radius=5)
            self.text(str(val) if val is not None else "--", x + 29, top + h + 28, 16, "white" if cur else "text",
                      cur, "center")
            self.hit(cr, "lane_clr", self.sel_lane, s)
            if cur:   # outline the whole column, bar and number, so it reads from across the room
                pygame.draw.rect(self.screen, C[ph["outline"]], self.r(x - 2, top - 2, 62, h + 52), ph["width"],
                                 border_radius=6)
        self.text("drag a step to set it; tap the number under it to clear", W - 16, 540, 14, "dim", False, "midright")

    def draw_macros(self):
        v = self.v
        for m, mc in enumerate(v["cfg"]["macro"]):
            x = 16 + m * 104
            self.button(x, 52, 84, 40, f"M{m + 1}", self.sel_macro == m, "mac_sel", m)
            self.vfader(x, 100, 84, 400, mc["value"], "mac_fader", m, "orange")
            self.text(str(mc["value"]), x + 42, 520, 20, "text", True, "center")
        px, mc = 440, v["cfg"]["macro"][self.sel_macro]
        self.text(f"M{self.sel_macro + 1} TARGETS", px, 58, 22, "text", True)
        self.text("LOW", px + 176, 92, 14, "dim")
        self.text("HIGH", px + 312, 92, 14, "dim")
        for t, mt in enumerate(mc["targets"]):
            y = 108 + t * 72
            self.button(px, y, 170, 60, trmap.label(mt["cc"]), False, "mt_tgt", self.sel_macro, t, size=16)
            if mt["cc"] is None:
                continue
            self.hslider(px + 176, y, 130, 60, mt["lo"], "mt_lo", self.sel_macro, t)
            self.hslider(px + 312, y, 130, 60, mt["hi"], "mt_hi", self.sel_macro, t)
            self.button(px + 448, y, 70, 60, au.CURVES[mt["curve"]], False, "mt_curve", self.sel_macro, t)
            self.button(px + 524, y, 50, 60, "X", False, "mt_clr", self.sel_macro, t, on_color="red")

    def draw_setup(self):
        v, s = self.v, self.v["settings"]
        self.text("PATTERN CH (sound CCs)", 20, 76, 18, "text", True, "midleft")
        self.button(270, 56, 50, 42, "-", False, "pat_ch", -1)
        self.text(str(s["pat_ch"]), 350, 77, 24, "white", True, "center")
        self.button(380, 56, 50, 42, "+", False, "pat_ch", 1)
        self.text("KIT CH", 20, 128, 18, "text", True, "midleft")
        self.button(270, 108, 50, 42, "-", False, "kit_ch", -1)
        self.text(str(s["kit_ch"]), 350, 129, 24, "white", True, "center")
        self.button(380, 108, 50, 42, "+", False, "kit_ch", 1)
        self.button(20, 166, 410, 50, "SEND ALL KNOWN VALUES", False, "send_all")
        self.button(20, 226, 410, 50, "FOLLOW PATTERN: ON" if s["follow"] else "FOLLOW PATTERN: OFF", s["follow"],
                    "follow", on_color="green")
        self.button(20, 286, 410, 50, f"SAVE AUTOMATION (slot {self.slot})", False, "save")
        self.button(20, 346, 200, 50, "EXIT APP", False, "exit", on_color="red")
        ph_name = s.get("playhead") if s.get("playhead") in PLAYHEAD else "ORANGE"   # old saves may say CYAN
        self.button(230, 346, 200, 50, f"PLAYHEAD: {ph_name}", False, "playhead_dd", size=14, caret=True)
        knob = s.get("knob", "floor")
        self.button(20, 406, 410, 44, "TR KNOB: FLOOR under automation" if knob == "floor" else "TR KNOB: PAUSES automation",
                    False, "knob_mode", size=14)
        known = sum(1 for x in v["value"] if x is not None)
        self.text(f"rx {v['rx']}  tx {v['tx']}  clk {v['clocks']}  notes {v['notes']}  known {known}/66", 20, 466, 16, "dim")
        self.text(f"port {v['port'] or '-- (waiting for the TR)'}", 20, 490, 16, "dim")
        self.text("slot 0 = default; pattern N uses slot N+1 when follow is on", 20, 514, 14, "dim")
        mx = 460
        pygame.draw.rect(self.screen, C["panel"], self.r(mx, 54, W - mx - 12, 490), border_radius=10)
        self.text("MIDI MONITOR", mx + 14, 64, 16, "dim", True)
        for i, (d, (st, d1, d2)) in enumerate(reversed(v["mon"])):
            typ, ch = st & 0xF0, (st & 0x0F) + 1
            if typ == 0xB0:
                what = f"CC{d1:<3} = {d2:<3} ch{ch}  {trmap.label(d1) if d1 in trmap.BY_CC else ''}"
            elif typ == 0xC0:
                what = f"PROG {d1 + 1} ch{ch}"
            else:
                what = {0xFA: "START", 0xFB: "CONTINUE", 0xFC: "STOP"}.get(st, f"{st:02X} {d1:02X} {d2:02X}")
                if st == 0xF2:
                    what = f"SONG POS {d1 | d2 << 7}"
            self.text(f"{d:<3} {what}", mx + 14, 92 + i * 28, 16, "orange" if d == "OUT" else "cyan")

    def draw_learn(self):
        """Tap a parameter, change it on the TR; what the TR reports is written under the name."""
        tr = self.v["tr"]
        for g, (group, names) in enumerate(LEARN_GROUPS):
            x = 12 + g * 336
            self.text(group, x + 4, 66, 18, "dim", True, "midleft")
            for k, name in enumerate(names):
                key, y = f"{group}/{name}", 84 + k * 50
                rec = self.learn.get(key)
                armed = self.learn_armed == key
                self.button(x, y, 150, 44, name, armed, "learn_arm", key, on_color="yellow", size=14, outline=bool(rec))
                if rec and rec["hits"]:
                    h = rec["hits"][0]
                    last = h["vals"][-1] if h["vals"] else None
                    v = (str(last[0]) if last and len(last) == 1 else str(last)[:14]) if last is not None else "?"
                    self.text(f"{h['blk']:02x}/{h['idx']} {h['off']:04x}", x + 158, y + 13, 14, "text", True, "midleft")
                    self.text(f"= {v}" + (f"  +{len(rec['hits']) - 1}" if len(rec["hits"]) > 1 else ""),
                              x + 158, y + 32, 14, "cyan", False, "midleft")
                elif armed:
                    self.text("change it on the TR now", x + 158, y + 22, 13, "yellow", False, "midleft")
        n = tr["notices"][-1] if tr["notices"] else None
        last = (f"last notice: blk {n[1]:02x} page {n[2]} idx {n[3]} off {n[4]:04x}..{n[5]:04x} = {n[6]}"[:110]
                if n else "no change notices yet" if tr["connected"] else "TR serial link down")
        self.text(last, 16, 536, 13, "dim", False, "midleft")
        if self.learn_armed:
            self.button(820, 492, 92, 40, "CLEAR", False, "learn_clear", size=14, on_color="red")
            self.button(920, 492, 92, 40, "DONE", False, "learn_done", size=14)

    def draw_picker(self):
        pygame.draw.rect(self.screen, C["bg"], self.r(0, 48, W, 502))
        inst = self.pick[2] if self.pick[0] == "lfo" and len(self.pick) > 2 else None
        if inst is not None:   # from the KIT page: only this instrument's parameters, big buttons
            self.text(f"{trmap.INST[inst]}: pick the parameter for LFO {self.pick[1] + 1}", 20, 70, 20, "text", True, "midleft")
            items = [(p[2], "pick", p[0]) for p in trmap.params_for_inst(inst)] + [("CANCEL", "pick_cancel", None)]
            for k, (label, kind, cc) in enumerate(items):
                self.button(20 + (k % 4) * 248, 100 + (k // 4) * 90, 236, 78, label, kind == "pick_cancel", kind, cc,
                            size=20, on_color="red")
            return
        cur = self.pick_current()
        if self.pick_page == 0:   # one column per instrument, in the TR's own track colours
            for i, inst in enumerate(trmap.INST):
                x = 8 + i * 101
                pygame.draw.rect(self.screen, C[self.track_colour(i)], self.r(x, 56, 95, 6), border_radius=3)
                self.text(inst, x + 47, 78, 18, "text", True, "center")
                for r, (cc, _, name, _) in enumerate(trmap.params_for_inst(i)):
                    self.button(x, 96 + r * 56, 95, 50, name, cc == cur, "pick", cc, size=14)
        else:                     # FX, grouped like the TR's panel
            for k, (title, names) in enumerate(FX_GROUPS):
                x = 8 + k * 169
                self.text(title, x + 81, 78, 18, "text", True, "center")
                for r, name in enumerate(names):
                    cc = next(p[0] for p in trmap.PARAMS if p[1] is None and p[2] == name)
                    self.button(x, 96 + r * 56, 162, 50, name, cc == cur, "pick", cc, size=15)
        self.button(8, 494, 240, 50, "FX  >" if self.pick_page == 0 else "<  INSTRUMENTS", False, "pick_page",
                    1 - self.pick_page, size=17)
        self.button(256, 494, 160, 50, "NONE", cur is None, "pick", None, size=17)
        self.text(f"now: {trmap.label(cur)}", 430, 519, 16, "dim", False, "midleft")
        self.button(856, 494, 160, 50, "CANCEL", True, "pick_cancel", on_color="red", size=17)

    def pick_current(self):
        p, cfg = self.pick, self.v["cfg"]
        if p[0] == "lfo":
            return cfg["lfo"][p[1]]["cc"]
        if p[0] == "lane":
            return cfg["lane"][p[1]]["cc"]
        return cfg["macro"][p[1]]["targets"][p[2]]["cc"]

    def draw(self):
        self.hits = []
        self.screen.fill(C["bg"])
        self.draw_tabs()
        if self.sound_pick:
            self.draw_sound_picker()
        elif self.pick:
            self.draw_picker()
        else:
            [self.draw_kit, self.draw_fx, self.draw_lfo, self.draw_lanes, self.draw_macros, self.draw_setup,
             self.draw_learn][self.page]()
        self.draw_transport()
        if self.dropdown:
            self.draw_dropdown()   # last, so it sits on top of everything (and its hits win)

    # -- touch
    def val_v(self, rect, y):
        return max(0, min(127, round(127 * (rect.bottom - y) / rect.height)))

    def val_h(self, rect, x):
        return max(0, min(127, round(127 * (x - rect.left) / rect.width)))

    def apply_drag(self, h, x, y):
        rect, kind, a, b = h
        sh = self.sh
        if kind == "vfader":
            nv = self.val_v(rect, y)
            if self.v["value"][a] != nv:
                self.queue_cc(a, nv)
                self.v["value"][a] = nv
        elif kind == "hslider":
            nv = self.val_h(rect, x)
            if self.v["value"][a] != nv:
                self.queue_cc(a, nv)
                self.v["value"][a] = nv
        elif kind == "fx_slider":   # serial FX parameter: its own range, latest per batch is sent
            _, _, lo, hi = ts.KIT_FX[a]
            nv = max(lo, min(hi, round(lo + (hi - lo) * (x - rect.left) / rect.width)))
            if self.v["tr"]["fx"].get(a) != nv:
                self.link.set_fx(a, nv)
                self.v["tr"]["fx"][a] = nv
        elif kind == "mix_slider":   # pan / sends, 0..1000 (b = mixer offset)
            nv = max(0, min(1000, round(1000 * (x - rect.left) / rect.width)))
            field = ts.MIX_FIELDS[b]
            if self.v["tr"]["tracks"][a].get(field) != nv:
                self.link.set_mixer(a, b, nv)
                self.v["tr"]["tracks"][a][field] = nv
        elif kind == "sc_depth":   # 0..1000, like the app; the link sends only the latest per batch
            nv = max(0, min(1000, round(1000 * (x - rect.left) / rect.width)))
            if self.v["tr"]["tracks"][a]["sc"] != nv:
                self.link.set_mixer(a, 0x0248, nv)
                self.v["tr"]["tracks"][a]["sc"] = nv
        elif kind == "mac_fader":
            nv = self.val_v(rect, y)
            with sh.lock:
                changed = sh.cfg["macro"][a]["value"] != nv
                sh.cfg["macro"][a]["value"] = nv
                outs = au.Engine.on_macro(sh.cfg["macro"][a]) if changed else []
            for cc, val in outs:
                self.queue_cc(cc, val)
        else:
            with sh.lock:
                cfg = sh.cfg
                if kind == "lfo_depth":
                    cfg["lfo"][a]["depth"] = self.val_h(rect, x)
                elif kind == "lfo_centre":
                    cfg["lfo"][a]["centre"] = self.val_h(rect, x)
                elif kind == "lane_step":
                    cfg["lane"][a]["steps"][b] = self.val_v(rect, y)
                elif kind == "mt_lo":
                    cfg["macro"][a]["targets"][b]["lo"] = self.val_h(rect, x)
                elif kind == "mt_hi":
                    cfg["macro"][a]["targets"][b]["hi"] = self.val_h(rect, x)

    def tap(self, h):
        rect, kind, a, b = h
        sh = self.sh
        if kind == "dd_close":
            self.dropdown = None
        elif kind == "dd_item":
            d, self.dropdown = self.dropdown, None
            if d["kind"] in ("shape", "rate", "step", "hz"):
                with sh.lock:
                    sh.cfg["lfo"][d["target"]][d["kind"]] = a
            elif d["kind"] == "sync":
                with sh.lock:
                    sh.cfg["lfo"][d["target"]]["sync"] = au.SYNCS[a]
            elif d["kind"] == "colour":
                self.link.set_mixer(d["target"], ts.COLOUR_OFF, a)
                self.v["tr"]["tracks"][d["target"]]["colour"] = a
            elif d["kind"] == "fx":
                self.link.set_fx(d["target"], a)
                self.v["tr"]["fx"][d["target"]] = a
            elif d["kind"] == "playhead":
                with sh.lock:
                    sh.settings["playhead"] = list(PLAYHEAD)[a]
                self.save_settings()
        elif kind == "lfo_rate":
            self.open_dropdown(*au.rate_options(self.v["cfg"]["lfo"][a])[:1], a, rect,
                               *au.rate_options(self.v["cfg"]["lfo"][a])[1:])
        elif kind == "lfo_sync":
            lf = self.v["cfg"]["lfo"][a]
            self.open_dropdown("sync", a, rect, au.SYNCS, au.SYNCS.index(lf.get("sync", "NOTE")))
        elif kind == "lfo_shape":
            self.open_dropdown("shape", a, rect, au.SHAPES, self.v["cfg"]["lfo"][a]["shape"])
        elif kind == "playhead_dd":
            names = list(PLAYHEAD)
            cur = self.v["settings"].get("playhead", "ORANGE")
            self.open_dropdown("playhead", None, rect, names, names.index(cur) if cur in names else 0)
        elif kind == "tab":
            self.page, self.pick, self.sound_pick = a, None, None
        elif kind == "inst_sel":
            # double tap on the selected instrument: its track colour, as set on the TR (mixer 024a)
            now = time.monotonic()
            if self.sel_inst == a and now - self._inst_tap < 0.45 and self.v["tr"]["connected"]:
                cur = self.v["tr"]["tracks"][a].get("colour")
                self.open_dropdown("colour", a, rect, ts.COLOURS, cur if cur is not None else -1, swatch=True)
                now = 0.0                  # a third tap starts over
            self.sel_inst, self._inst_tap = a, now
        elif kind == "lfo_tgt_inst":
            self.pick = ("lfo", a, self.sel_inst)
        elif kind == "lfo_add":
            self.pick = ("lfo", a, self.sel_inst, "add")   # picking a parameter also switches it on
        elif kind == "layer_pick":
            # Open on what the layer plays now. An OFF (or unread) layer opens where Layer A is,
            # so an empty Layer B starts among the track's own kind of sound, not on OTHER/OFF.
            src, cat = self.sp_place(a) or self.sp_place(0) or ("sounds", None)
            self.sound_pick = dict(track=self.sel_inst, layer=a, src=src, cat=cat, off=[0, 0], max=[0, 0])
            cats = self.sp_lists()[0]
            if cat not in cats:
                self.sound_pick["cat"] = cats[0] if cats else 0
            self.sp_centre()
        elif kind == "sp_src":
            # Keep the category when the other list has one of the same name (BD E exists in both,
            # under different ids: 1 for sounds, 65 for samples).
            tr, sp = self.v["tr"], self.sound_pick
            name = tr["categories"].get(sp["cat"])
            sp.update(src=("sounds", "samples")[a], off=[0, 0])
            cats = self.sp_lists()[0]
            same = [c for c in cats if tr["categories"].get(c) == name]
            sp["cat"] = same[0] if same and name else (cats[0] if cats else 0)
            self.sp_centre()
        elif kind == "zero":
            cur = self.zero_value(a)
            if cur != 0:
                if cur is not None:
                    self.zero_prev[a] = cur
                self.zero_set(a, 0)
            elif a in self.zero_prev:
                self.zero_set(a, self.zero_prev.pop(a))
        elif kind == "fx_tab":
            self.fx_tab = a
            self.tr_follow(a)
        elif kind == "fx_choice":
            self.link.set_fx(a, b)
            self.v["tr"]["fx"][a] = b
        elif kind == "fx_dd":
            opts = FX_CHOICES[a]
            cur = self.v["tr"]["fx"].get(a)
            self.open_dropdown("fx", a, rect, opts, cur if cur is not None and 0 <= cur < len(opts) else -1)
        elif kind == "fx_route":
            self.link.set_mixer(self.sel_inst, 0x0247, a)
        elif kind == "sp_close":
            self.sound_pick = None
        elif kind == "sp_jump":   # UP / DOWN under a list: one screen
            sp = self.sound_pick
            sp["off"][a] = max(0, min(sp["max"][a], sp["off"][a] + b * SP_H))
        elif kind == "toggle":
            val = self.v["value"][a]
            self.queue_cc(a, 0 if val is not None and val >= 64 else 127)
        elif kind in ("lfo_tgt", "lane_tgt"):
            self.pick, self.pick_page = (kind[:4].rstrip("_"), a), 0
        elif kind == "mt_tgt":
            self.pick, self.pick_page = ("macro", a, b), 0
        elif kind == "pick_page":
            self.pick_page = a
        elif kind == "lane_sel":
            self.sel_lane = a
        elif kind == "mac_sel":
            self.sel_macro = a
        elif kind == "save":
            with sh.lock:
                slot = sh.pattern + 1 if sh.settings["follow"] and sh.pattern is not None else 0
            self.save_slot(slot)
        elif kind == "send_all":
            with sh.lock:
                vals = [(cc, v) for cc, v in enumerate(sh.value) if v is not None]
            self.worker.engine.invalidate()
            for cc, v in vals:
                self.queue_cc(cc, v)
        elif kind == "exit":
            self.running = False
        elif kind == "learn_arm":
            self.learn_armed = None if self.learn_armed == a else a
            self.learn_seq = self.v["tr"]["notice_seq"]   # only notices from now on count
        elif kind == "learn_clear":
            self.learn.pop(self.learn_armed, None)
            self.save_learn()
        elif kind == "learn_done":
            self.learn_armed = None
        elif kind == "hold":
            with sh.lock:
                sh.hold = not sh.hold
            if not sh.hold:
                self.worker.engine.invalidate()   # resend the current values straight away
        elif kind == "pick_cancel":
            self.pick = None
        else:
            with sh.lock:
                cfg, s = sh.cfg, sh.settings
                if kind == "lfo_en":
                    x = cfg["lfo"][a]
                    x["enabled"], x["paused"] = not x["enabled"], False
                elif kind == "lfo_arm":
                    cfg["lfo"][a]["paused"] = False
                elif kind == "lane_en":
                    x = cfg["lane"][a]
                    x["enabled"], x["paused"] = not x["enabled"], False
                elif kind == "lane_len":
                    x = cfg["lane"][a]
                    x["length"] = 4 if x["length"] >= 16 else x["length"] + 4
                elif kind == "lane_arm":
                    cfg["lane"][a]["paused"] = False
                elif kind == "lane_clr":
                    cfg["lane"][a]["steps"][b] = None
                elif kind == "mt_curve":
                    t = cfg["macro"][a]["targets"][b]
                    t["curve"] = (t["curve"] + 1) % 3
                elif kind == "mt_clr":
                    cfg["macro"][a]["targets"][b] = dict(cc=None, lo=0, hi=127, curve=0)
                elif kind in ("pat_ch", "kit_ch"):
                    s[kind] = (s[kind] - 1 + a) % 16 + 1
                elif kind == "follow":
                    s["follow"] = not s["follow"]
                elif kind == "knob_mode":
                    s["knob"] = "pause" if s.get("knob", "floor") == "floor" else "floor"
                    for x in cfg["lfo"] + cfg["lane"]:
                        x["paused"] = False
                elif kind == "pick":
                    p = self.pick
                    if p[0] == "lfo":
                        cfg["lfo"][p[1]].update(cc=a, paused=False)
                        if len(p) > 3 and a is not None:   # "+ ADD LFO" on the KIT page
                            cfg["lfo"][p[1]]["enabled"] = True
                    elif p[0] == "lane":
                        cfg["lane"][p[1]].update(cc=a, paused=False)
                    else:
                        cfg["macro"][p[1]]["targets"][p[2]]["cc"] = a
                    self.pick = None
            if kind in ("lfo_arm", "lane_arm"):
                self.worker.engine.invalidate()
            if kind in ("pat_ch", "kit_ch", "follow", "knob_mode"):
                self.save_settings()

    def find(self, pos):
        for h in reversed(self.hits):   # last drawn is on top
            if h[0].collidepoint(pos):
                return h
        return None

    # -- touch, as three steps so the picker lists can tell a scroll from a tap
    def press(self, pos):
        h = self.find(pos)
        if not h:
            return
        if h[1] == "sp_list":
            self.list_touch = dict(li=h[2], pos=pos, off=self.sound_pick["off"][h[2]], moved=False)
        elif h[1] in self.DRAG:
            self.drag = h
            self.apply_drag(h, *pos)
        else:
            self.tap(h)

    def move(self, pos):
        lt = self.list_touch
        if lt and self.sound_pick:
            dy = (pos[1] - lt["pos"][1]) / self.sy
            if abs(dy) > SP_TAP_SLOP:
                lt["moved"] = True
            if lt["moved"]:
                sp = self.sound_pick
                sp["off"][lt["li"]] = max(0, min(sp["max"][lt["li"]], lt["off"] - dy))
        elif self.drag:
            self.apply_drag(self.drag, *pos)

    def release(self):
        lt, self.list_touch, self.drag = self.list_touch, None, None
        if lt and self.sound_pick and not lt["moved"]:
            self.list_tap(lt["li"], lt["pos"][0] / self.sx, lt["pos"][1] / self.sy)

    # -- main loop
    def run(self):
        os.environ.setdefault("SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS", "0")
        pygame.init()
        self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        pygame.display.set_caption("TR-1000 Companion")
        pygame.mouse.set_visible(False)
        sw, shh = self.screen.get_size()
        self.sx, self.sy = sw / W, shh / H
        print(f"[ui] screen {sw}x{shh}", flush=True)
        self.worker.start()
        self.link.start()
        clock = pygame.time.Clock()
        self.running = True
        self.v = self.snapshot()
        while self.running:
            touched = False
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                    self.running = False
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    self.press(ev.pos)
                    touched = True
                elif ev.type == pygame.MOUSEMOTION and (self.drag or self.list_touch):
                    self.move(ev.pos)
                    touched = True
                elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                    self.release()
                    touched = True
            with self.sh.lock:
                pending, self.sh.pending_pattern = self.sh.pending_pattern, None
            if pending is not None:
                self.load_slot(pending + 1)   # no preset for this pattern: keep the current set
            self.v = self.snapshot()
            self.learn_poll()
            self.draw()
            pygame.display.flip()
            clock.tick(60 if touched or self.drag or self.list_touch else 20)
        pygame.quit()


def main():
    print(f"[boot] tr-companion, python {sys.version.split()[0]}", flush=True)
    App().run()
