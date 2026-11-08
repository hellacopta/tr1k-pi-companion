"""TR-1000 control map, from the MIDI Implementation Chart v1.11 (Oct. 1, 2025) in this folder.

Every CC here is transmitted AND recognized by the TR. Channel: MEASURED 2026-10-28 over USB,
all knob CCs arrive on the Pattern channel (10), see ../captures/tr-capture-20261028.txt.

"""

INST = ["BD", "SD", "LT", "HT", "RS", "HC", "CH", "OH", "CC", "RC"]

# (cc, instrument index or None for FX, name, toggle)
PARAMS = []


def _inst(i, ccs, names):
    for cc, name in zip(ccs, names):
        PARAMS.append((cc, i, name, False))


_SEVEN = ["TUNE", "DECAY", "MIX", "CTRL1", "CTRL2", "CTRL3", "LEVEL"]
_FOUR = ["TUNE", "DECAY", "CTRL", "LEVEL"]
_inst(0, range(22, 29), _SEVEN)
_inst(1, [29, 30, 31, 46, 47, 48, 49], _SEVEN)   # the chart skips 32-45
_inst(2, range(50, 57), _SEVEN)
_inst(3, range(57, 64), _SEVEN)
_inst(4, range(80, 84), _FOUR)
_inst(5, range(84, 88), _FOUR)
_inst(6, range(102, 106), _FOUR)
_inst(7, range(106, 110), _FOUR)
_inst(8, range(110, 114), _FOUR)
_inst(9, range(114, 118), _FOUR)
PARAMS += [
    (20, None, "FILTER", False), (21, None, "DRIVE", False),
    (19, None, "AFX ON", True), (15, None, "MFX ON", True),
    (16, None, "MFX C1", False), (17, None, "MFX C2", False), (18, None, "MFX C3", False),
    (12, None, "DLY LVL", False), (13, None, "DLY TIME", False), (14, None, "DLY FB", False),
    (90, None, "REV TIME", False), (91, None, "REV LVL", False),
    (89, None, "MORPH", False), (9, None, "EXT IN", False),
]

BY_CC = {p[0]: p for p in PARAMS}


def params_for_inst(i):
    return [p for p in PARAMS if p[1] == i]


def level_cc(i):
    return next(p[0] for p in PARAMS if p[1] == i and p[2] == "LEVEL")


def label(cc):
    p = BY_CC.get(cc)
    if p is None:
        return "-- none --"
    return f"{INST[p[1]] if p[1] is not None else 'FX'} {p[2]}"
