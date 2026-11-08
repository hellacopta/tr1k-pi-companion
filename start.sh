#!/bin/sh
# Start (or restart) the TR-1000 companion on the Pi's own screen. Safe to run over SSH.
# Tracks the app by PID file -- `pkill -f run.py` over SSH matches the SSH shell's own command
# line and kills it (hit 2026-10-28).
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
DIR=$(dirname "$(readlink -f "$0")")
PIDF="$HOME/.cache/tr-companion.pid"
LOG="$HOME/.cache/tr-companion.log"
mkdir -p "$HOME/.cache"
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  kill "$(cat "$PIDF")"
  sleep 1
fi
setsid python3 "$DIR/run.py" >> "$LOG" 2>&1 < /dev/null &
echo $! > "$PIDF"
