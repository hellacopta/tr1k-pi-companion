"""Clock-synced LFOs, step lanes and macros.

Config is plain dicts/lists so a whole set saves as JSON. The Engine turns clock ticks into
(cc, value) pairs, sends only on change, caps output per tick, and pauses an LFO/lane when the
TR reports that its CC was turned by hand ("knob takeover").
"""
import copy
import math
import random

LFOS, LANES, MACROS, MACRO_TARGETS, LANE_STEPS = 4, 4, 4, 6, 16
SHAPES = ["SINE", "TRI", "SAW UP", "SAW DN", "SQUARE", "S&H"]
RATE16 = [1, 2, 4, 8, 16, 32, 64]
RATE_NAMES = ["1/16", "1/8", "1/4", "1/2", "1 BAR", "2 BAR", "4 BAR"]
# LFO sync, named like the TR's own LFO (TIME / STEP / NOTE, from the TR-1000 App's tables).
# NOTE: a note value (RATE_NAMES). STEP: a cycle of N sixteenth steps. TIME: free-running in Hz,
# off the wall clock instead of the TR's MIDI clock (still only while the TR plays).
SYNCS = ["NOTE", "STEP", "TIME"]
STEP_OPTS = [1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 16, 24, 32, 48, 64]
STEP_NAMES = [f"{n} STEP" if n == 1 else f"{n} STEPS" for n in STEP_OPTS]
HZ_OPTS = [0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 3, 4, 6, 8]
HZ_NAMES = [f"{h:g} Hz" for h in HZ_OPTS]
TIME_TICK_S = 0.01   # how often TIME LFOs are evaluated
CURVES = ["LIN", "EXP", "LOG"]
BUDGET_PER_TICK = 8
# A CC arriving from the TR with a value we sent it this recently is the TR repeating our own
# message back (Soft Through, or its edit-data echo), not a hand on a knob. Without this an armed
# LFO or lane paused itself again on its first send (reported 2026-11-05: ARM did not resume).
ECHO_S = 0.15
# What a TR knob does to automation on that CC (SETUP > KNOB). "pause" is the original knob
# takeover. "floor" (the user's request, 2026-11-08) keeps the lane/LFO running and uses the knob
# as a floor under it: knob 0 -> the lane's values as they are; knob 50 + lane 0 -> 50; knob 127
# -> the lane changes nothing. The floor is the knob's last value, so turning it back down clears it.
KNOB_MODES = ("floor", "pause")
VERSION = 1


def defaults():
    return {
        "version": VERSION,
        "lfo": [dict(enabled=False, cc=None, shape=0, rate=4, depth=32, centre=64, paused=False,
                     sync="NOTE", step=STEP_OPTS.index(16), hz=HZ_OPTS.index(1))
                for _ in range(LFOS)],
        "lane": [dict(enabled=False, cc=None, length=16, paused=False, steps=[None] * LANE_STEPS)
                 for _ in range(LANES)],
        "macro": [dict(value=0, targets=[dict(cc=None, lo=0, hi=127, curve=0) for _ in range(MACRO_TARGETS)])
                  for _ in range(MACROS)],
    }


def clamp7(v):
    return 0 if v < 0 else 127 if v > 127 else int(v + 0.5)


def wave(shape, ph, held):
    if shape == 0:
        return math.sin(ph * 2 * math.pi)
    if shape == 1:
        return ph * 4 if ph < 0.25 else 2 - ph * 4 if ph < 0.75 else ph * 4 - 4
    if shape == 2:
        return ph * 2 - 1
    if shape == 3:
        return 1 - ph * 2
    if shape == 4:
        return 1.0 if ph < 0.5 else -1.0
    return held


def rate_options(lf):
    """(dropdown kind, option names, current index) for an LFO's rate under its sync mode."""
    sync = lf.get("sync", "NOTE")
    if sync == "STEP":
        return "step", STEP_NAMES, lf.get("step", STEP_OPTS.index(16))
    if sync == "TIME":
        return "hz", HZ_NAMES, lf.get("hz", HZ_OPTS.index(1))
    return "rate", RATE_NAMES, lf["rate"]


def rate_label(lf):
    _, names, i = rate_options(lf)
    return names[i % len(names)]


def macro_value(t, pos):
    x = pos / 127
    if t["curve"] == 1:
        x = x * x
    elif t["curve"] == 2:
        x = math.sqrt(x)
    return clamp7(t["lo"] + (t["hi"] - t["lo"]) * x)


class Engine:
    def __init__(self, seed=0x2545F491):
        self.rng = random.Random(seed)
        self.reset()

    def reset(self):
        self.last_sent = {}
        self.held = [0.0] * LFOS
        self.last_cycle = [None] * LFOS
        self.t0 = None
        self.recent = {}          # cc -> {value: when we last sent it}, for is_echo()
        self.now = None
        self.floor = {}           # cc -> the TR knob's last value, applied in "floor" mode
        self.t0 = None            # wall-clock start of TIME LFOs (reset on Start)

    def invalidate(self):
        self.last_sent = {}

    def note_sent(self, cc, v, now):
        """Remember a CC we sent (automation or a panel fader) so its echo is recognised."""
        if now is None:
            return
        r = self.recent.setdefault(cc, {})
        r[v] = now
        if len(r) > 32:           # an LFO sweeps through many values: keep only the fresh ones
            for k in [k for k, t in r.items() if now - t > ECHO_S]:
                del r[k]

    def is_echo(self, cc, v, now):
        t = self.recent.get(cc, {}).get(v)
        return t is not None and now is not None and 0 <= now - t <= ECHO_S

    def on_incoming_cc(self, cfg, cc, value=None, now=None, mode="pause"):
        """A CC from the TR. Returns False for the echo of a value we just sent (nothing changes).
        Otherwise it is a hand on the knob: in "pause" mode whatever drives that CC pauses (knob
        takeover); in "floor" mode the knob's value becomes the floor under that automation."""
        if value is not None and self.is_echo(cc, value, now):
            return False
        if mode == "floor":
            if value is not None:
                self.floor[cc] = value
        else:
            for x in cfg["lfo"] + cfg["lane"]:
                if x["enabled"] and x["cc"] == cc:
                    x["paused"] = True
        self.last_sent.pop(cc, None)
        return True

    def _emit(self, out, cc, v):
        v = max(v, self.floor.get(cc, 0))
        if len(out) >= BUDGET_PER_TICK or self.last_sent.get(cc) == v:
            return
        self.last_sent[cc] = v
        self.note_sent(cc, v, self.now)
        out.append((cc, v))

    def on_tick(self, cfg, ticks, now=None, knob="pause"):
        self.now = now
        if knob != "floor":
            self.floor = {}
        out = []
        if ticks % 6 == 0:   # lanes first: a late step is audible, a late LFO point is not
            step = ticks // 6
            for ln in cfg["lane"]:
                if ln["enabled"] and not ln["paused"] and ln["cc"] is not None and ln["length"]:
                    v = ln["steps"][step % ln["length"]]
                    if v is not None:
                        self._emit(out, ln["cc"], v)
        for i, lf in enumerate(cfg["lfo"]):
            if not lf["enabled"] or lf["paused"] or lf["cc"] is None:
                continue
            sync = lf.get("sync", "NOTE")
            if sync == "TIME":
                continue                      # on_time() runs these
            if sync == "STEP":
                period = STEP_OPTS[lf.get("step", 10) % len(STEP_OPTS)] * 6
            else:
                period = RATE16[lf["rate"] % len(RATE16)] * 6
            cycle = ticks // period
            if cycle != self.last_cycle[i]:
                self.last_cycle[i] = cycle
                self.held[i] = self.rng.uniform(-1, 1)
            ph = (ticks % period) / period
            self._emit(out, lf["cc"], clamp7(lf["centre"] + lf["depth"] * wave(lf["shape"], ph, self.held[i])))
        return out

    def on_time(self, cfg, now, knob="pause"):
        """TIME-synced LFOs, from the wall clock. Called every TIME_TICK_S while the TR plays."""
        self.now = now
        if knob != "floor":
            self.floor = {}
        if self.t0 is None:
            self.t0 = now
        out = []
        for i, lf in enumerate(cfg["lfo"]):
            if not lf["enabled"] or lf["paused"] or lf["cc"] is None or lf.get("sync", "NOTE") != "TIME":
                continue
            cyc = (now - self.t0) * HZ_OPTS[lf.get("hz", 5) % len(HZ_OPTS)]
            cycle = int(cyc)
            if cycle != self.last_cycle[i]:
                self.last_cycle[i] = cycle
                self.held[i] = self.rng.uniform(-1, 1)
            self._emit(out, lf["cc"], clamp7(lf["centre"] + lf["depth"] * wave(lf["shape"], cyc - cycle, self.held[i])))
        return out

    @staticmethod
    def on_macro(macro):
        return [(t["cc"], macro_value(t, macro["value"])) for t in macro["targets"] if t["cc"] is not None]


def load(d):
    """Validated copy of a saved config, or None."""
    if not isinstance(d, dict) or d.get("version") != VERSION:
        return None
    c = copy.deepcopy(d)
    for x in c["lfo"] + c["lane"]:
        x["paused"] = False
    for x in c["lfo"]:                       # saved before sync modes existed (2026-11-08)
        x.setdefault("sync", "NOTE")
        x.setdefault("step", STEP_OPTS.index(16))
        x.setdefault("hz", HZ_OPTS.index(1))
    return c
