#!/usr/bin/env python3
"""Entry point: python3 ~/tr-companion/run.py (started by labwc autostart, see ../README.md)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tr_companion.app import main  # noqa: E402

main()
