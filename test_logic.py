"""Tests for the Pi companion's pure logic. Plain stdlib, no pytest: python3 pi/test_logic.py
Exits non-zero on failure and prints the derivation next to each check.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tr_companion import automation as au  # noqa: E402
from tr_companion import trmap  # noqa: E402
from tr_companion.clock import Clock  # noqa: E402
from tr_companion.midi import Parser  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    if not cond:
        fails += 1
        print("  FAIL", msg)


def feed_all(p, data):
    out = []
    for b in data:
        m = p.feed(b)
        if m:
            out.append(m)
    return out


print("trmap")
check(len(trmap.PARAMS) == 66, f"66 params (CC rows 9,12-31,46-63,80-87,89-91,102-117), got {len(trmap.PARAMS)}")
check(len(trmap.BY_CC) == 66, "no CC listed twice")
check([p[0] for p in trmap.params_for_inst(1)][3] == 46, "SD CTRL1 is CC 46")
check(trmap.level_cc(9) == 117 and trmap.label(89) == "FX MORPH", "RC LEVEL 117, CC 89 labelled FX MORPH")
# Every CC seen from the TR in the 2026-10-28 capture is in the map.
for cc in (20, 21, 24, 27, 28, 49, 56, 63, 83, 87, 89, 105, 109, 113, 116, 117):
    check(cc in trmap.BY_CC, f"captured CC {cc} mapped")

print("midi parser")
p = Parser()
got = feed_all(p, [0xB9, 20, 64, 21, 0xF8, 16])
check(got == [(0xB9, 20, 64), (0xF8, 0, 0), (0xB9, 21, 16)], f"CC, clock mid-message, running-status CC: {got}")
got = feed_all(p, [0xC9, 5, 6])
check(got == [(0xC9, 5, 0), (0xC9, 6, 0)], "program change 5 then 6 by running status")
got = feed_all(p, [0xF2, 2, 1, 0x40])
check(len(got) == 1 and got[0][1] | got[0][2] << 7 == 130, "SPP = 2 + 1*128 = 130, trailing byte not run on F2")
got = feed_all(p, [0xF0, 0xF7, 0xB9, 9, 127])   # the TR's empty SysEx, seen in the capture
check(got == [(0xB9, 9, 127)], "empty SysEx skipped, then CC 9=127")

print("clock")
c = Clock()
c.on_start()
t = 1.0
for _ in range(96):
    c.on_tick(t)
    t += 0.0208333   # 120 BPM: 2 beats/s * 24 = 48 ticks/s
bpm = c.bpm(t)
check(119.5 < bpm < 120.5, f"tempo 60/(0.0208333*24) = 120.0, got {bpm:.2f}")
check(c.ticks == 96 and c.step16 == 16, "96 ticks = 16 sixteenths")
check(c.bpm(t + 0.6) == 0, "tempo 0 once the clock is silent > 0.5 s")

print("automation")
cfg = au.defaults()
e = au.Engine()
ln = cfg["lane"][0]
ln.update(enabled=True, cc=23, length=16)
ln["steps"][0], ln["steps"][4] = 100, 20
sends = {}
for tick in range(96):
    for cc, v in e.on_tick(cfg, tick):
        if cc == 23:
            sends.setdefault(tick, []).append(v)
check(sends == {0: [100], 24: [20]}, f"lane sends once, on steps 0 and 4 (ticks 0 and 24): {sends}")

cfg = au.defaults()
e = au.Engine()
cfg["lfo"][0].update(enabled=True, cc=20, shape=4, rate=2, depth=40, centre=64)
check(e.on_tick(cfg, 0) == [(20, 104)], "square first half = 64+40 = 104")
check(e.on_tick(cfg, 5) == [], "no resend while unchanged")
check(e.on_tick(cfg, 12) == [(20, 24)], "square second half = 64-40 = 24 (period 1/4 = 24 ticks)")

cfg = au.defaults()
e = au.Engine()
cfg["lfo"][1].update(enabled=True, cc=21, shape=0, rate=4, depth=63, centre=64)
check(e.on_tick(cfg, 24) == [(21, 127)], "sine at 1/4 of a 96-tick bar = 64+63 = 127")
e.on_incoming_cc(cfg, 21)
check(cfg["lfo"][1]["paused"] and e.on_tick(cfg, 48) == [], "TR knob on CC 21 pauses that LFO")
# Armed again: the TR repeating our own value back must not pause it; a different value must.
cfg["lfo"][1]["paused"] = False
e.invalidate()
sent = e.on_tick(cfg, 24, now=10.0)
check(sent == [(21, 127)] and e.on_incoming_cc(cfg, 21, 127, 10.01) is False and not cfg["lfo"][1]["paused"],
      "echo of the value just sent (10 ms later) is not a knob: LFO keeps running")
check(e.on_incoming_cc(cfg, 21, 90, 10.02) is True and cfg["lfo"][1]["paused"], "another value 20 ms later is a knob: paused")
cfg["lfo"][1]["paused"] = False
check(e.on_incoming_cc(cfg, 21, 127, 10.0 + au.ECHO_S + 0.1) is True and cfg["lfo"][1]["paused"],
      "the same value after the echo window is a knob again")
# Floor mode: the knob does not pause; its value is a floor under the lane (user's spec 2026-11-08).
cfg["lane"][0].update(enabled=True, cc=23, length=4, paused=False)
cfg["lane"][0]["steps"][:4] = [127, 0, 60, None]
cfg["lfo"][1]["paused"] = False
e.reset()
e.on_incoming_cc(cfg, 23, 50, 20.0, "floor")                      # knob at 50
check(not cfg["lane"][0]["paused"], "floor mode: the knob does not pause the lane")
got = [e.on_tick(cfg, t, 20.0 + t / 100, "floor") for t in (0, 6, 12)]
lane = [[x for x in o if x[0] == 23] for o in got]
check(lane == [[(23, 127)], [(23, 50)], [(23, 60)]], f"knob 50: lane 127 -> 127, lane 0 -> 50, lane 60 -> 60: {lane}")
e.on_incoming_cc(cfg, 23, 127, 21.0, "floor")                     # knob maxed: the lane changes nothing
e.invalidate()
got = [[x for x in e.on_tick(cfg, t, 21.0 + t / 100, "floor") if x[0] == 23] for t in (24, 30)]
check(got == [[(23, 127)], []], f"knob 127: 127 once, then nothing: {got}")
e.on_incoming_cc(cfg, 23, 0, 22.0, "floor")                       # back to 0: lane plays as set
e.invalidate()
check([x for x in e.on_tick(cfg, 30, 22.1, "floor") if x[0] == 23] == [(23, 0)], "knob 0: lane 0 plays as 0")
e.on_incoming_cc(cfg, 23, 90, 22.3, "floor")
e.invalidate()
check([x for x in e.on_tick(cfg, 6, 22.4, "pause") if x[0] == 23] == [(23, 0)] and e.floor == {},
      "pause mode clears the floors: lane 0 goes out as 0 despite the knob at 90")

cfg = au.defaults()
e = au.Engine()
for i in range(4):
    cfg["lfo"][i].update(enabled=True, cc=22 + i, shape=2, rate=0, depth=60)
    cfg["lane"][i].update(enabled=True, cc=50 + i)
    cfg["lane"][i]["steps"][0] = 99
check(len(e.on_tick(cfg, 0)) <= au.BUDGET_PER_TICK, "per-tick budget respected")

t = dict(cc=20, lo=0, hi=100, curve=1)
half = au.macro_value(t, 64)
check(au.macro_value(t, 127) == 100 and au.macro_value(t, 0) == 0 and 24 <= half <= 27,
      f"exp macro: ends 0..100, middle 100*(64/127)^2 = 25.4, got {half}")
saved = au.defaults()
saved["lfo"][0]["paused"] = True
check(au.load(saved)["lfo"][0]["paused"] is False and au.load({"version": 99}) is None,
      "loading clears pause flags and rejects other versions")

print("tr serial (against bytes captured from the Roland app, 2026-10-28)")
from tr_companion import trserial as ts  # noqa: E402
# The app's read of the current kit: header bytes 4-11 differ (stale memory), payload must match.
cap_req = bytes.fromhex("15 08 41 f2 f7 7f 00 00 00 95 83 e8 0b 00 00 00 82 03 00 00 00 00 00 72 00 01 00")
ours = ts.frame15(0x08, ts.read_request(0x03, 0x00, 0, 0x0072, 1))
check(len(ours) == len(cap_req) and ours[:4] == cap_req[:4] and ours[12:] == cap_req[12:],
      f"read-kit frame matches the app's except header bytes 4-11: {ours.hex(' ')}")
# The app's Layer A sound-id read for BD on kit 18 and the TR's reply framing.
reply = bytes.fromhex("15 08 f0 00 d8 74 a8 86 01 00 00 00 0f 00 00 00 02 03 00 00 00 00 00 cc 00 01 00 01 00 00 00")
d = ts.Deframer()
fr = d.feed(reply[:7]) + d.feed(reply[7:] + bytes.fromhex("14 08 f0 00 07 68 00 34 00 00 00 05"))
check(len(fr) == 2 and d.skipped == 0 and fr[1][0] == 0x14, "deframer: split 15-frame + a 12-byte 14-frame")
blk, page, idx, off, vals = ts.parse_read_reply(ts.payload(fr[0]))
check((blk, page, idx, off, vals) == (0x03, 0, 0, 0x00CC, [1]), f"reply parsed: {(blk, page, idx, off, vals)}")
# Pages are u16 LE in bytes 3-4: sample slot 353 (0x161) answered with 0x01 in byte 4 (probe_slots.py).
req = ts.read_request(0x9C, 353, 0, 0x02E7, 214)
check(req.hex(" ") == "82 9c 00 61 01 00 00 e7 02 d6 00", f"u16 page read request: {req.hex(' ')}")
check(ts.parse_read_reply(bytes([0x02]) + req[1:])[1] == 353, "u16 page parsed back")
# Name block of BD as the TR sent it after BD -> 808 Snare Drum (27 u32 chars from 024d).
vals = [0x38, 48, 56, 32, 83, 110, 97, 114, 101, 32, 68, 114, 117, 109, 32, 32, 83, 68, 32, 69, 32, 32, 32, 32]
check(ts.chars(vals[:16]).rstrip() == "808 Snare Drum" and ts.chars(vals[16:]).rstrip() == "SD E",
      "name block decodes to '808 Snare Drum' / 'SD E'")
check(ts.track_blocks(0) == (0x10, 0x11) and ts.track_blocks(1) == (0x1A, 0x1B) and ts.track_blocks(9)[0] == 0x6A,
      "track blocks: BD 10/11, SD 1a/1b (captured), RC 6a")

# Write side, against the app's frames for BD -> 27 8X Claves on kit 65 (page 0x41).
fr = ts.load_sound_frames(0, 0x41, 27, "8X Claves", "WOOD E")
check(ts.payload(fr[0]) == bytes.fromhex("09 1b 00 00 00 00 00 00 00"), "op 09 matches the capture")
check(ts.payload(fr[1]) == bytes.fromhex("01 11 00 41 00 00 00 6d 0b 1b 00 00 00"), "id write matches the capture")
check(ts.payload(fr[2]) == bytes.fromhex("01 10 00 41 00 00 00 4d 02 38 00 00 00"), "first name char '8' at 024d")
check(len(fr) == 26 and ts.payload(fr[-1])[7:9] == (0x0264).to_bytes(2, "little"), "24 name chars, last at 0264")
check(ts.load_request(19, 1) == bytes.fromhex("09 13 00 00 00 01 00 00 00"), "op 09 for SD <- 19 matches the capture")

# Catalogue entries exactly as the TR sent them at connect (2026-10-28 capture).
e = bytes.fromhex("06 01 00 01 00 01 00 00 00 01 00") + b"808 Bass Drum   "
check(ts.parse_sound_entry(e) == (1, 1, 1, "808 Bass Drum"), f"sound entry: {ts.parse_sound_entry(e)}")
check(ts.parse_sound_entry(bytes(266)) is None, "266-byte sample entries are skipped")
k = bytes.fromhex("0c 00 00") + b"Dub Techno Kit  "
check(ts.parse_named_entry(k) == (0, "Dub Techno Kit"), "kit list entry")
c = bytes.fromhex("08 01 00") + b"BD E" + bytes(12)
check(ts.parse_named_entry(c) == (1, "BD E"), "category entry")
kf = ts.kit_change_frames(0x41)
check(ts.payload(kf[0]) == bytes.fromhex("01 03 00 00 00 00 00 72 00 41 00 00 00") and
      ts.payload(kf[1]) == bytes.fromhex("01 74 00 00 00 00 00 06 04 41 00 00 00"), "kit change matches the capture")

# Layer B and samples, against the 2026-10-28 captures on kit 90 (page 0x59).
fb = ts.load_sound_frames(0, 0x59, 805, "Deep SH Bass", "BASS", layer=1)
check(len(fb) == 2 and ts.payload(fb[0]) == bytes.fromhex("09 25 03 00 00 00 00 01 00") and
      ts.payload(fb[1]) == bytes.fromhex("01 11 00 59 00 01 00 6d 0b 25 03 00 00"), "Layer B <- 805 matches, no name write")
fo = ts.load_sound_frames(0, 0x59, 0, "OFF", "", layer=1)
check(ts.payload(fo[0]) == bytes.fromhex("09 00 00 00 00 00 00 01 00") and
      ts.payload(fo[1]) == bytes.fromhex("01 11 00 59 00 01 00 6d 0b 00 00 00 00"), "Layer B <- OFF matches")
fs = ts.load_sound_frames(1, 0x59, 3001, "Pulse", "FX E")
check(ts.payload(fs[0]) == bytes.fromhex("09 e8 03 b9 0b 01 00 00 00"), "sample load: instrument 1000 + sample 3001, SD")
check(len(fs) == 25 and ts.payload(fs[1]) == bytes.fromhex("01 1a 00 59 00 00 00 4d 02 50 00 00 00"),
      "sample: no sound-id write (the TR sets 1000 itself), name 'P' first at 1a/024d")
se = bytes.fromhex("06 b9 0b 5b 00 0f 00 00 00 01 00") + b"A:/Roland/TR-1000/SAMPLE/VARIOUS/FX E/Pulse.SMP\x00" + bytes(266 - 11 - 48)
check(ts.parse_sample_entry(se) == (3001, 91, 15, "Pulse", "A:/Roland/TR-1000/SAMPLE/VARIOUS/FX E/Pulse.SMP", True),
      f"sample entry: {ts.parse_sample_entry(se)}")

# Mixer writes, against the capture (kit 14 = page 0d, SD = index 1).
check(ts.payload(ts.mixer_write_frame(1, 0x0d, ts.FX_ROUTE_OFF, 2)) == bytes.fromhex("01 0d 00 0d 00 01 00 47 02 02 00 00 00"),
      "FX route SD -> ANALOG matches the capture")
check(ts.payload(ts.mixer_write_frame(1, 0x0d, ts.SC_DEPTH_OFF, 428))[7:13] == bytes.fromhex("48 02 ac 01 00 00"),
      "SC depth 42.8 % = 428 at 0248")
check(ts.FX_ROUTES[0] == "THROUGH" and ts.FX_ROUTES[1] == "MASTER" and ts.FX_ROUTES[2] == "ANALOG", "route values 0/1/2")
# Kit FX addresses: every key must match what the TR reported on the LEARN page (2026-11-08),
# every key must be inside its block's read window, and the option lists must span the ranges.
import json as _json
_learn = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures", "learn-20261108.json")
if os.path.exists(_learn):
    _names = {"REVERB/LEVEL": "rev_level", "REVERB/TIME": "rev_time", "REVERB/PRE DLY": "rev_predly",
              "REVERB/LOW CUT": "rev_locut", "REVERB/HIGH CUT": "rev_hicut", "REVERB/TYPE": "rev_type",
              "REVERB/DENSITY": "rev_density", "REVERB/SC DEPTH": "rev_sc", "REVERB/FX ROUTE": "rev_route",
              "DELAY/LEVEL": "dly_level", "DELAY/TIME": "dly_time", "DELAY/FEEDBACK": "dly_fb", "DELAY/SYNC": "dly_sync",
              "DELAY/TYPE": "dly_type", "DELAY/SC DEPTH": "dly_sc", "DELAY/RVB SEND": "dly_rvb",
              "DELAY/FX ROUTE": "dly_route", "SIDE CHAIN/SOURCE": "sc_source", "SIDE CHAIN/CURVE": "sc_curve",
              "SIDE CHAIN/MODE": "sc_mode", "SIDE CHAIN/MODE TIME": "sc_modetime", "SIDE CHAIN/RELEASE": "sc_release",
              "SIDE CHAIN/GATE/DUCK": "sc_gateduck", "SIDE CHAIN/MSTR DEP": "sc_mstr"}
    with open(_learn) as f:
        _got = {_names[k]: (v["hits"][0]["blk"], v["hits"][0]["off"]) for k, v in _json.load(f).items()}
    _bad = [k for k, (b, o) in _got.items() if ts.KIT_FX[k][:2] != (b, o)]
    check(len(_got) == 24 and not _bad, f"KIT_FX matches the 24 learned addresses (mismatch: {_bad})")
_outside = [k for k, (b, o, lo, hi) in ts.KIT_FX.items() if not (ts.FX_BLOCKS[b][0] <= o < ts.FX_BLOCKS[b][0] + ts.FX_BLOCKS[b][1])]
check(not _outside, f"every KIT_FX key is inside its block's read window: {_outside}")
check(len(ts.REVERB_TYPES) == 6 and len(ts.DELAY_TYPES) == 4 and len(ts.LOW_CUTS) == 17 and len(ts.HIGH_CUTS) == 15
      and len(ts.SC_SOURCES) == 19 and ts.KIT_FX["sc_source"][3] == 18 and len(ts.SC_MODES) == len(ts.SC_GATEDUCK) == 2,
      "option lists span the ranges (0-5, 0-3, 0-16, 0-14, source 0-18, two-way mode and gate/duck)")
check(ts.MIX_WINDOW == (0x0244, 7) and ts.MIX_FIELDS[0x0247] == "route" and ts.MIX_FIELDS[0x024A] == "colour",
      "mixer window 0244 x7 covers pan, sends, route, SC depth, colour")
# LFO sync modes. STEP: 4 steps = 24 ticks per cycle; square = high for the first 12.
e = au.Engine()
cfg = au.defaults()
cfg["lfo"][0].update(enabled=True, cc=20, shape=4, depth=40, centre=64, sync="STEP", step=au.STEP_OPTS.index(4))
check(e.on_tick(cfg, 0) == [(20, 104)] and e.on_tick(cfg, 12) == [(20, 24)] and e.on_tick(cfg, 24) == [(20, 104)],
      "STEP sync: a 4-step square flips at tick 12 and again at 24")
cfg["lfo"][0].update(sync="TIME", hz=au.HZ_OPTS.index(1))
e.reset()
check(e.on_tick(cfg, 0) == [], "TIME sync ignores clock ticks")
check(e.on_time(cfg, 100.0) == [(20, 104)] and e.on_time(cfg, 100.6) == [(20, 24)] and e.on_time(cfg, 101.1) == [(20, 104)],
      "TIME sync at 1 Hz: high for 0.5 s from the first call, low at 0.6 s, high again at 1.1 s")
check(au.rate_label(cfg["lfo"][0]) == "1 Hz" and au.rate_label(dict(rate=2)) == "1/4", "rate labels follow the sync mode")
_old = au.defaults()
for _x in _old["lfo"]:
    del _x["sync"], _x["step"], _x["hz"]
check(au.load(_old)["lfo"][0]["sync"] == "NOTE", "presets saved before sync modes load as NOTE")
check(ts.payload(ts.fx_write_frame("rev_type", 44, 9))[9:13] == (5).to_bytes(4, "little") and
      ts.payload(ts.fx_write_frame("rev_level", 44, 760)) == bytes.fromhex("01 05 00 2c 00 00 00 5b 09 f8 02 00 00"),
      "fx write: clamped to the range; reverb level 760 on kit 44 -> block 05 offset 095b")

if fails:
    print(f"{fails} check(s) FAILED")
    sys.exit(1)
print("all checks passed")
