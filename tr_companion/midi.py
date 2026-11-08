"""MIDI byte parser and the TR-1000's raw ALSA port.

The TR's USB MIDI is picked up by Linux's generic Roland autodetect (snd-usb-audio, vendor
interface with the <06 24 F1 02 in out> descriptor) as card "TR1000" with two rawmidi
subdevices; everything the TR sends arrives on the first ("TR-1000 MIDI 1"), MEASURED
2026-10-28. Using the raw device needs no python-rtmidi (the pi user has no passwordless sudo).

Parser: running status, real-time bytes in the middle of
other messages, SysEx skipped (the TR sends an empty F0 F7 now and then).
"""
import os
import re


def data_len(status):
    hi = status & 0xF0
    if hi in (0xC0, 0xD0):
        return 1
    if hi == 0xF0:
        return {0xF1: 1, 0xF3: 1, 0xF2: 2}.get(status, 0)
    return 2


class Parser:
    def __init__(self):
        self.running = 0
        self.need = 0
        self.data = []
        self.in_sysex = False
        self.runs = False

    def feed(self, b):
        """Feed one byte; return (status, d1, d2) when a message completes, else None."""
        if b >= 0xF8:
            return None if b in (0xF9, 0xFD) else (b, 0, 0)
        if b == 0xF0:
            self.in_sysex, self.running, self.data = True, 0, []
            return None
        if b == 0xF7:
            self.in_sysex = False
            return None
        if self.in_sysex:
            if b & 0x80:
                self.in_sysex = False
            else:
                return None
        if b & 0x80:
            self.running, self.data = b, []
            self.need = data_len(b)
            self.runs = b < 0xF0
            if self.need == 0:
                self.running = 0
                return (b, 0, 0)
            return None
        if not self.running:
            return None
        self.data.append(b)
        if len(self.data) < self.need:
            return None
        d = self.data
        msg = (self.running, d[0], d[1] if self.need > 1 else 0)
        self.data = []
        if not self.runs:
            self.running = 0
        return msg


def find_tr_rawmidi():
    """Path of the TR-1000's first rawmidi device, or None if it is not plugged in."""
    try:
        cards = open("/proc/asound/cards").read()
    except OSError:
        return None
    m = re.search(r"^\s*(\d+)\s+\[TR1000\s*\]", cards, re.M)
    if not m:
        return None
    path = f"/dev/snd/midiC{m.group(1)}D0"
    return path if os.path.exists(path) else None


class TRPort:
    """Non-blocking read/write on the TR's rawmidi device."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def read(self):
        try:
            return os.read(self.fd, 256)
        except BlockingIOError:
            return b""

    def write(self, data):
        os.write(self.fd, bytes(data))

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
