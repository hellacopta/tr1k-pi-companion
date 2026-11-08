"""Headless layout check: draws every page with sample data and saves PNGs, without a screen,
a TR or the MIDI thread. Runs on the PC or the Pi:

    python3 pi/smoke_render.py <out_dir> [width height]

Exits non-zero if any page raises. Look at the PNGs before deploying a layout change.
"""
import os
import sys

os.environ["SDL_VIDEODRIVER"] = "dummy"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pygame  # noqa: E402

from tr_companion import app as appmod  # noqa: E402
from tr_companion import trmap  # noqa: E402

out = sys.argv[1] if len(sys.argv) > 1 else "."
w, h = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (1024, 600)
os.makedirs(out, exist_ok=True)
appmod.CONF_FILE = os.path.join(out, "presets-smoke.json")   # never touch the real presets

pygame.init()
a = appmod.App()
a.screen = pygame.display.set_mode((w, h))
a.sx, a.sy = w / appmod.W, h / appmod.H

# Sample state: some values known from the TR, an LFO and a lane running, one macro wired.
sh = a.sh
for i, cc in enumerate([20, 21, 24, 28, 49, 56, 63, 83, 87, 105, 109, 113, 117, 15]):
    sh.value[cc], sh.source[cc] = (i * 37) % 128, "tr"
sh.value[20], sh.source[20] = 90, "auto"
cfg = sh.cfg
cfg["lfo"][0].update(enabled=True, cc=20, shape=0, rate=2, depth=40, centre=64)
cfg["lfo"][1].update(enabled=True, cc=28, shape=4, rate=4, depth=20, centre=100, paused=True)
cfg["lane"][0].update(enabled=True, cc=23, length=12)
for s, v in [(0, 110), (3, 40), (4, 90), (8, 127), (10, 20)]:
    cfg["lane"][0]["steps"][s] = v
cfg["macro"][0]["value"] = 80
cfg["macro"][0]["targets"][0].update(cc=20, lo=10, hi=110, curve=1)
cfg["macro"][0]["targets"][1].update(cc=91, lo=0, hi=90, curve=0)
sh.clock.running, sh.clock.ticks, sh.kit, sh.pattern = True, 6 * 4, 2, 11   # step 5 (index 4) has a value
sh.mon.extend([("IN", (0xB9, 105, 13)), ("IN", (0xFA, 0, 0)), ("OUT", (0xB9, 20, 90)), ("IN", (0xC9, 11, 0))])
sh.port_path, sh.rx, sh.notes = "/dev/snd/midiC3D0", 4125, 153
# TR over serial, as probe_layers.py read kit 90 (internal 89) on 2026-10-28.
trs = sh.trs
trs.connected, trs.status, trs.firmware, trs.kit = True, "connected", "1.50", 89
trs.kit_names = {13: "Deep Techno Kit", 44: "TR-909-1000", 89: ""}
trs.categories = {1: "BD E", 3: "SD E", 5: "TOM E", 7: "STICK E", 9: "WOOD E", 10: "CLAP"}
trs.sounds = {1: (1, 1, "808 Bass Drum"), 2: (3, 1, "808 Snare Drum"), 11: (1, 1, "909 Bass Drum"),
              17: (1, 2, "8X Bass Drum"), 18: (3, 2, "8X Snare Drum"), 19: (5, 2, "8X Tom"),
              20: (7, 2, "8X Rim Shot"), 21: (10, 2, "8X Hand Clap"), 27: (9, 2, "8X Claves")}
trs.sounds.update({100 + k: (1, 3, f"Kick {k:02d}") for k in range(30)})   # enough BD E sounds to page
# The TR's catalogue has its own "OFF" as sound 0, alone in category OTHER; the picker hides both.
trs.sounds[0] = (51, 7, "OFF")
trs.categories.update({51: "OTHER", 52: "BD A", 65: "BD E", 91: "FX E"})   # samples use other ids: BD E is 65
trs.samples = {1000: (52, 8, "Regular Kick 01", True), 1001: (52, 8, "Regular Kick 02", True),
               1281: (65, 8, "Lofi Kick 01", True),
               3001: (91, 15, "Pulse", True), 3600: (91, 15, "My Riser", False)}
_cols = iter([0, 11, 5, 5, 1, 2, 9, 6, 3, 10])   # TR colour per track, as COLOURS indices


def track(a, name, cat, b=0, **kw):
    return dict(dict(a=a, b=b, a_sample=None, b_sample=None, name=name, cat=cat, route=1, sc=0, colour=next(_cols),
                     pan=500, rsend=357, dsend=0), **kw)


# SD's Layer B plays a sample (the BD keeps Layer B OFF for the OFF-picker check below).
trs.tracks = [track(27, "8X Claves", "WOOD E"),
              track(18, "SD", "SD E", b=1000, b_sample=3001, route=2, sc=428)] + \
    [track(sid, n, c) for sid, n, c in [(19, "LT", "TOM E"), (19, "HT", "TOM E"), (20, "RS", "STICK E"),
                                        (21, "HC", "CLAP"), (22, "CH", "HIHAT E"), (23, "OH", "HIHAT E"),
                                        (24, "CC", "CRASH E")]] + \
    [track(1000, "Pulse", "FX E", a_sample=3001)]
trs.sample_by_path = {"A:/Roland/TR-1000/SAMPLE/FX/Pulse.SMP": 3001}

fails = 0
LFO, LANES = appmod.PAGES.index("LFO"), appmod.PAGES.index("LANES")


def shot(name, page, pick=None, dropdown=None, playhead="ORANGE"):
    """Draw one state and save it; returns True on success."""
    global fails
    try:
        a.page, a.pick, a.dropdown = page, pick, None
        sh.settings["playhead"] = playhead
        a.v = a.snapshot()
        if dropdown:   # open it the way a tap would: from the button's hit rect
            a.draw()
            h = next(x for x in a.hits if x[1] == dropdown)
            a.tap(h)
        a.draw()
        pygame.image.save(a.screen, os.path.join(out, f"page-{name}.png"))
        print(f"ok   {name:14} hits={len(a.hits)}")
        return True
    except Exception as e:  # report every state, not just the first failure
        fails += 1
        print(f"FAIL {name}: {e!r}")
        return False


trs.fx = {"rev_type": 2, "rev_route": 1, "rev_level": 760, "rev_time": 430, "rev_sc": 0, "rev_predly": 48,
          "rev_locut": 4, "rev_hicut": 14, "rev_density": 7, "dly_type": 2, "dly_sync": 1, "dly_level": 559,
          "dly_rvb": 281, "dly_route": 0, "dly_sc": 320, "dly_time": 573, "dly_fb": 488, "sc_source": 1,
          "sc_modetime": 255, "sc_release": 478, "sc_curve": 699, "sc_gateduck": 1, "sc_mstr": 380, "sc_mode": 0}
for i, p in enumerate(appmod.PAGES):
    shot(p.lower(), i)
FXP = appmod.PAGES.index("FX")
for a.fx_tab, nm in ((1, "fx-delay"), (2, "fx-sidechain"), (3, "fx-master")):
    shot(nm, FXP)
a.fx_tab = 0
shot("fx-lowcut-dropdown", FXP, dropdown="fx_dd")    # LOW CUT: 17 options wrap into two columns
try:
    a.page, a.pick, a.dropdown, a.fx_tab = FXP, None, None, 0
    a.v = a.snapshot()
    a.draw()
    a.tap(next(x for x in a.hits if x[1] == "fx_choice" and x[2] == "rev_route" and x[3] == 2))   # ANALOG
    h = next(x for x in a.hits if x[1] == "fx_slider" and x[2] == "rev_sc")
    a.apply_drag(h, h[0].left + h[0].width // 2, h[0].centery)
    a.draw()
    a.tap(next(x for x in a.hits if x[1] == "fx_dd" and x[2] == "rev_type"))
    a.draw()
    a.tap(next(x for x in reversed(a.hits) if x[1] == "dd_item" and x[2] == 4))                   # PLATE
    cmds = [a.link.cmds.get_nowait() for _ in range(3)]
    ok = cmds[0] == ("fx", "rev_route", 2) and cmds[1][:2] == ("fx", "rev_sc") and 495 <= cmds[1][2] <= 505 \
        and cmds[2] == ("fx", "rev_type", 4) and a.dropdown is None
    print(f"{'ok  ' if ok else 'FAIL'} fx serial -> {cmds}")
    fails += 0 if ok else 1
    labels = [h[2] for h in a.hits if h[1] in ("fx_slider", "fx_dd", "fx_choice")]
    ok = "rev_level" not in labels and "rev_time" not in labels
    print(f"{'ok  ' if ok else 'FAIL'} fx: reverb level/time left to the TR")
    fails += 0 if ok else 1
    # Picking DELAY sends the delay-level CC at the current level (559/1000 -> 71) and queues the exact
    # value to be written back over serial.
    while not a.txq.empty():
        a.txq.get_nowait()
    a.tap(next(x for x in a.hits if x[1] == "fx_tab" and x[2] == 1))
    sent, later = a.txq.get_nowait(), a.link.cmds.get_nowait()
    ok = sent[1:] == (12, 71) and later[:3] == ("fx_at", "dly_level", 559)
    print(f"{'ok  ' if ok else 'FAIL'} fx follow -> {sent} then {later[:3]}")
    fails += 0 if ok else 1
    a.tap(next(x for x in a.hits if x[1] == "fx_tab" and x[2] == 2))              # SIDE CHAIN: no CC to send
    ok = a.txq.empty() and a.link.cmds.empty()
    print(f"{'ok  ' if ok else 'FAIL'} fx follow: side chain sends nothing")
    fails += 0 if ok else 1
    a.draw()
    labels = {h[2]: h[3] for h in a.hits if h[1] == "fx_choice" and h[2] in ("sc_mode", "sc_gateduck")}
    ok = set(labels) == {"sc_mode", "sc_gateduck"}
    print(f"{'ok  ' if ok else 'FAIL'} side chain mode / gate-duck are named buttons")
    fails += 0 if ok else 1
    a.fx_tab = 0
except Exception as e:
    fails += 1
    print(f"FAIL fx serial: {e!r}")
shot("picker", LFO, ("lfo", 0))
shot("picker-kit", 0, ("lfo", 1, 0))            # LFO 2 from the KIT page, BD params only
a.pick_page = 1
shot("picker-fx", LFO, ("lfo", 0))              # page 2 of the full picker: FX by section
a.pick_page = 0
shot("lfo-dropdown", LFO, dropdown="lfo_shape")
shot("lfo-rate-dropdown", LFO, dropdown="lfo_rate")
a.sel_lane = 1                                   # lane 1 runs, lane 2 is selected: lane 1 keeps its outline
shot("lanes-other", LANES)
a.sel_lane = 0
shot("lanes-yellow", LANES, playhead="YELLOW")
shot("lanes-dim", LANES, playhead="DIM")
shot("setup-dropdown", appmod.PAGES.index("SETUP"), dropdown="playhead_dd")
# LEARN: arm REVERB/TIME, let two notices arrive, they bind to it; a notice before arming does not.
try:
    appmod.LEARN_FILE = os.path.join(out, "learn-smoke.json")
    a.learn = {}
    trs.notices.append((1, 0x05, 18, 0, 0x095C, 0x095C, [600]))
    trs.notice_seq = 1
    a.page, a.pick, a.dropdown = appmod.PAGES.index("LEARN"), None, None
    a.v = a.snapshot()
    a.learn_poll()
    a.draw()
    a.tap(next(x for x in a.hits if x[1] == "learn_arm" and x[2] == "REVERB/TIME"))
    trs.notices.append((2, 0x05, 18, 0, 0x095C, 0x095C, [760]))
    trs.notices.append((3, 0x05, 18, 0, 0x095C, 0x095C, [1000]))
    trs.notice_seq = 3
    a.v = a.snapshot()
    a.learn_poll()
    a.draw()
    pygame.image.save(a.screen, os.path.join(out, "page-learn.png"))
    rec = a.learn.get("REVERB/TIME")
    ok = rec and len(rec["hits"]) == 1 and rec["hits"][0]["off"] == 0x095C and rec["hits"][0]["vals"] == [[760], [1000]] \
        and os.path.exists(appmod.LEARN_FILE)
    print(f"{'ok  ' if ok else 'FAIL'} learn   {rec}")
    fails += 0 if ok else 1
    a.learn_armed = None
except Exception as e:
    fails += 1
    print(f"FAIL learn: {e!r}")
# KIT page sound picker. Its two lists scroll by dragging and pick on a tap, except in the right
# part (SP_DEAD) of every column, where a tap never picks.
def list_pos(li, idx, frac=0.4):
    """Screen position inside entry idx of picker list li, `frac` across its column, at the current scroll."""
    x, cols, cw = a.sp_geom(li)
    row, c = divmod(idx, cols)
    return (round((x + c * cw + cw * frac) * a.sx),
            round((appmod.SP_Y + row * appmod.SP_ROW + 22 - a.sound_pick["off"][li]) * a.sy))


def rail_pos(li, y):
    """A point in the scroll-only right part of list li's first column."""
    x, cols, cw = a.sp_geom(li)
    return round((x + cw * (1 - appmod.SP_DEAD / 2)) * a.sx), round(y * a.sy)


def click(pos):
    a.press(pos)
    a.release()


def check(name, ok, detail=""):
    global fails
    print(f"{'ok  ' if ok else 'FAIL'} {name} {detail}")
    fails += 0 if ok else 1


def open_picker(inst, layer):
    a.sel_inst, a.sound_pick = inst, None
    a.v = a.snapshot()
    a.draw()
    a.tap(next(x for x in a.hits if x[1] == "layer_pick" and x[2] == layer))
    a.draw()
    return a.sound_pick


try:
    a.page, a.pick, a.dropdown = 0, None, None
    sp = open_picker(0, 0)                                                        # BD Layer A = 8X Claves
    check("picker opens on Layer A's sound", (sp["track"], sp["layer"], sp["src"], sp["cat"]) == (0, 0, "sounds", 9))
    pygame.image.save(a.screen, os.path.join(out, "page-kit-picker.png"))
    cats, _ = a.sp_lists()
    check("OTHER (the TR's own OFF) is not a category", 51 not in cats, cats)
    click(list_pos(0, cats.index(1)))                                             # BD E
    a.draw()
    check("category tap", a.sound_pick["cat"] == 1)
    for li in (0, 1):                                                             # taps in the rails
        click(rail_pos(li, appmod.SP_Y + 70))
    check("dead-zone taps pick nothing", a.sound_pick is not None and a.sound_pick["cat"] == 1 and a.link.cmds.empty())
    click(list_pos(1, 0, 0.72))                                                   # just inside the dead zone
    check("tap at 72% of a column picks nothing", a.sound_pick is not None and a.link.cmds.empty())
    p0 = list_pos(1, 4)                                                           # drag the sounds up 100
    a.press(p0)
    a.move((p0[0], p0[1] - round(60 * a.sy)))
    a.move((p0[0], p0[1] - round(100 * a.sy)))
    a.release()
    a.draw()
    check("drag scrolls, picks nothing", a.sound_pick is not None and abs(a.sound_pick["off"][1] - 100) < 2
          and a.link.cmds.empty(), a.sound_pick and a.sound_pick["off"])
    p0 = rail_pos(1, 300)                                                         # drag in the dead zone: down 60
    a.press(p0)
    a.move((p0[0], p0[1] + round(60 * a.sy)))
    a.release()
    a.draw()
    check("dead-zone drag scrolls", abs(a.sound_pick["off"][1] - 40) < 2, a.sound_pick["off"])
    a.tap(next(x for x in a.hits if x[1] == "sp_jump" and x[2] == 1 and x[3] == 1))   # DOWN: one screen
    a.draw()
    pygame.image.save(a.screen, os.path.join(out, "page-kit-picker-scrolled.png"))
    ids = [sid for sid, _ in a.sp_lists()[1]]
    p0 = list_pos(1, ids.index(125))                                              # "Kick 25", far down the list
    a.press(p0)
    a.move((p0[0], p0[1] + round(6 * a.sy)))                                      # a wobble is still a tap
    a.release()
    cmd = a.link.cmds.get_nowait()
    check("pick after scrolling", cmd == ("load", 0, 0, 125) and a.sound_pick is None, cmd)
    # Layer B, OFF: it used to open on OTHER showing OFF twice. Now it opens where Layer A is.
    sp = open_picker(0, 1)
    _, items = a.sp_lists()
    check("OFF Layer B opens where Layer A is", (sp["src"], sp["cat"]) == ("sounds", 9) and a.sp_current() == 0
          and items == [(0, "OFF"), (27, "8X Claves")], (sp["src"], sp["cat"], items))
    pygame.image.save(a.screen, os.path.join(out, "page-kit-picker-layerB.png"))
    click(list_pos(1, 0))                                                         # OFF
    cmd = a.link.cmds.get_nowait()
    check("layer B -> OFF", cmd == ("load", 0, 1, 0), cmd)
    # Samples: SD Layer A (SD E). SAMPLES has no SD E here, so it falls back to the first category.
    open_picker(1, 0)
    a.tap(next(x for x in a.hits if x[1] == "sp_src" and x[2] == 1))
    check("SAMPLES without a same-name category", a.sound_pick["cat"] == 52, a.sound_pick["cat"])
    cats, _ = a.sp_lists()
    a.draw()
    click(list_pos(0, cats.index(91)))                                            # FX E
    a.draw()
    pygame.image.save(a.screen, os.path.join(out, "page-kit-samples.png"))
    ids = [sid for sid, _ in a.sp_lists()[1]]
    click(list_pos(1, ids.index(3001)))                                           # Pulse
    cmd = a.link.cmds.get_nowait()
    check("samples -> Pulse", cmd == ("load", 1, 0, 3001), cmd)
    # SOUNDS > BD E, then SAMPLES: stays on BD E, which is id 65 among the samples.
    open_picker(1, 0)
    click(list_pos(0, a.sp_lists()[0].index(1)))
    a.draw()
    a.tap(next(x for x in a.hits if x[1] == "sp_src" and x[2] == 1))
    check("SAMPLES keeps the category name", a.sound_pick["cat"] == 65 and a.sp_lists()[1] == [(1281, "Lofi Kick 01")],
          a.sound_pick["cat"])
    # A layer playing a sample opens on that sample, not on sample id 1000.
    sp = open_picker(1, 1)                                                        # SD Layer B = Pulse
    check("sample layer opens on its sample", (sp["src"], sp["cat"], a.sp_current()) == ("samples", 91, 3001),
          (sp["src"], sp["cat"], a.sp_current()))
    a.sound_pick = None
    # A track holding a sample (RC reads 1000) shows the sample's name from its name block.
    a.sel_inst = 9
    a.v = a.snapshot()
    a.draw()
    pygame.image.save(a.screen, os.path.join(out, "page-kit-sample-track.png"))
    lbl = [a.layer_text(a.v["tr"]["tracks"][9], 0), a.layer_text(a.v["tr"]["tracks"][1], 1)]
    ok = lbl == [("3001  Pulse", "FX E"), ("3001  Pulse", "FX E")]
    print(f"{'ok  ' if ok else 'FAIL'} sample labels {lbl}")
    fails += 0 if ok else 1
    # Mixer: FX route buttons and the SC depth slider (SD shows ANALOG, 42.8 %).
    a.sel_inst = 1
    a.v = a.snapshot()
    a.draw()
    pygame.image.save(a.screen, os.path.join(out, "page-kit-mixer.png"))
    a.tap(next(x for x in a.hits if x[1] == "fx_route" and x[2] == 0))
    h = next(x for x in a.hits if x[1] == "sc_depth")
    a.apply_drag(h, h[0].left + h[0].width * 3 // 4, h[0].centery)
    cmds = [a.link.cmds.get_nowait() for _ in range(2)]
    ok = cmds[0] == ("mixer", 1, 0x0247, 0) and cmds[1][:3] == ("mixer", 1, 0x0248) and 740 <= cmds[1][3] <= 760
    print(f"{'ok  ' if ok else 'FAIL'} mixer -> {cmds}")
    fails += 0 if ok else 1
    # Double tap SD (already selected): colour picker; pick CYAN (5). A single tap does not open it.
    a.sel_inst, a._inst_tap = 1, 0.0
    a.v = a.snapshot()
    a.draw()
    a.tap(next(x for x in a.hits if x[1] == "inst_sel" and x[2] == 1))
    one = a.dropdown
    a.tap(next(x for x in a.hits if x[1] == "inst_sel" and x[2] == 1))
    a.draw()
    pygame.image.save(a.screen, os.path.join(out, "page-kit-colour.png"))
    a.tap(next(x for x in reversed(a.hits) if x[1] == "dd_item" and x[2] == 5))
    cmd = a.link.cmds.get_nowait()
    ok = one is None and cmd == ("mixer", 1, 0x024A, 5) and a.dropdown is None
    print(f"{'ok  ' if ok else 'FAIL'} double tap -> colour {cmd}")
    fails += 0 if ok else 1
    a.v = a.snapshot()
    a.draw()
    h = next(x for x in a.hits if x[1] == "mix_slider" and x[3] == 0x0244)        # pan, drag to 3/4
    a.apply_drag(h, h[0].left + h[0].width * 3 // 4, h[0].centery)
    cmd = a.link.cmds.get_nowait()
    ok = cmd[:3] == ("mixer", 1, 0x0244) and 740 <= cmd[3] <= 760
    print(f"{'ok  ' if ok else 'FAIL'} pan -> {cmd}")
    fails += 0 if ok else 1
    a.sel_inst = 0
except Exception as e:
    fails += 1
    print(f"FAIL kit picker: {e!r}")
# Choosing from the wave dropdown must change the shape and close it.
try:
    a.page, a.pick, a.dropdown = LFO, None, None
    a.v = a.snapshot()
    a.draw()
    a.tap(next(x for x in a.hits if x[1] == "lfo_shape" and x[2] == 0))
    a.draw()
    a.tap(next(x for x in reversed(a.hits) if x[1] == "dd_item" and x[2] == 5))   # S&H
    ok = sh.cfg["lfo"][0]["shape"] == 5 and a.dropdown is None
    print(f"{'ok  ' if ok else 'FAIL'} dropdown pick -> shape {sh.cfg['lfo'][0]['shape']}")
    fails += 0 if ok else 1
    a.draw()
    a.tap(next(x for x in a.hits if x[1] == "lfo_rate" and x[2] == 0))
    a.draw()
    a.tap(next(x for x in reversed(a.hits) if x[1] == "dd_item" and x[2] == 6))   # 4 BAR
    ok = sh.cfg["lfo"][0]["rate"] == 6 and a.dropdown is None
    print(f"{'ok  ' if ok else 'FAIL'} rate dropdown -> {sh.cfg['lfo'][0]['rate']}")
    fails += 0 if ok else 1
    # Tapping a slider's name zeroes it; tapping again restores the value it had.
    depth0 = sh.cfg["lfo"][0]["depth"]
    a.v = a.snapshot()
    a.draw()
    a.tap(next(x for x in a.hits if x[1] == "zero" and x[2] == ("depth", 0)))
    zeroed = sh.cfg["lfo"][0]["depth"]
    a.v = a.snapshot()
    a.draw()
    a.tap(next(x for x in a.hits if x[1] == "zero" and x[2] == ("depth", 0)))
    ok = depth0 == 40 and zeroed == 0 and sh.cfg["lfo"][0]["depth"] == 40
    print(f"{'ok  ' if ok else 'FAIL'} name tap depth 40 -> {zeroed} -> {sh.cfg['lfo'][0]['depth']}")
    fails += 0 if ok else 1
    while not a.link.cmds.empty():
        a.link.cmds.get_nowait()
    a.page, a.fx_tab = appmod.PAGES.index("FX"), 3   # MASTER: the MIDI-only controls
    a.v = a.snapshot()
    a.draw()
    a.tap(next(x for x in a.hits if x[1] == "zero" and x[2] == ("cc", 20)))   # FILTER, was 90
    sent = []
    while not a.txq.empty():
        sent.append(a.txq.get_nowait())
    ok = bool(sent) and sent[-1][1:] == (20, 0)
    print(f"{'ok  ' if ok else 'FAIL'} name tap FILTER -> {sent[-1:]}")
    fails += 0 if ok else 1
    a.draw()
    pygame.image.save(a.screen, os.path.join(out, "page-fx-zeroed.png"))
except Exception as e:
    fails += 1
    print(f"FAIL dropdown pick: {e!r}")
# Drive the MIDI worker's message handler with what the TR actually sent on 2026-10-28 (no port,
# no thread): a knob CC on ch 10, start, clock ticks with an LFO running, an empty SysEx and stop.
# A name clash with threading.Thread internals (Thread._handle in 3.13) killed this on the Pi.
try:
    worker = appmod.MidiWorker(sh, a.txq)
    sh.clock = appmod.Clock()
    for m in [(0xB9, 105, 13), (0xFA, 0, 0)] + [(0xF8, 0, 0)] * 30 + [(0xFC, 0, 0)]:
        worker.on_msg(m, 1.0)
    ok = sh.value[105] == 13 and sh.clock.ticks == 30 and not sh.clock.running and sh.clocks >= 30
    print(f"{'ok  ' if ok else 'FAIL'} midi    ticks={sh.clock.ticks} cc105={sh.value[105]}")
    fails += 0 if ok else 1
    # HOLD: with the clock running, automation sends nothing; releasing it resends at once.
    sh.clock = appmod.Clock()
    sh.value[20], sh.source[20] = None, ""
    a.page, a.pick, a.dropdown = 0, None, None
    a.v = a.snapshot()
    a.draw()
    a.tap(next(x for x in a.hits if x[1] == "hold"))
    worker.on_msg((0xFA, 0, 0), 2.0)
    for _ in range(12):
        worker.on_msg((0xF8, 0, 0), 2.0)
    held = sh.value[20]
    a.tap(next(x for x in a.hits if x[1] == "hold"))
    worker.on_msg((0xF8, 0, 0), 2.0)
    ok = held is None and sh.value[20] is not None and not sh.hold
    print(f"{'ok  ' if ok else 'FAIL'} hold    during={held} after={sh.value[20]}")
    fails += 0 if ok else 1
except Exception as e:
    fails += 1
    print(f"FAIL midi worker: {e!r}")
# The labels in the parameter picker must all be distinct, or two targets would look the same.
labels = [trmap.label(p[0]) for p in trmap.PARAMS]
if len(set(labels)) != len(labels):
    fails += 1
    print("FAIL duplicate picker labels")
sys.exit(1 if fails else 0)
