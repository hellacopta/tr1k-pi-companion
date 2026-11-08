"""Follows the TR-1000's MIDI clock (24 ppqn)."""

TICKS_PER_16TH = 6
STALE_S = 0.5


class Clock:
    def __init__(self):
        self.running = False
        self.ticks = 0
        self.last_tick = None
        self.interval = 0.0
        self.just_started = False

    def on_tick(self, now):
        if self.last_tick is not None:
            dt = now - self.last_tick
            if 0 < dt < STALE_S:
                # EMA over ~24 ticks: steady readout, still follows tempo changes
                self.interval = dt if self.interval == 0 else self.interval + (dt - self.interval) / 24
        self.last_tick = now
        if self.running:
            self.ticks += 1

    def on_start(self):
        self.running, self.ticks, self.just_started = True, 0, True

    def on_continue(self):
        self.running = True

    def on_stop(self):
        self.running = False

    def on_song_position(self, sixteenths):
        self.ticks = sixteenths * TICKS_PER_16TH

    @property
    def step16(self):
        return self.ticks // TICKS_PER_16TH

    def bpm(self, now):
        if self.interval <= 0 or self.last_tick is None or now - self.last_tick > STALE_S:
            return 0.0
        return 60.0 / (self.interval * 24)
