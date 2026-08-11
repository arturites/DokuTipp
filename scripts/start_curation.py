#!/usr/bin/env python3
"""Legacy compatibility wrapper for the DokuTipp default workflow."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from dokutipp.cli import run_default


if __name__ == "__main__":
    run_default(data_dir=PROJECT_ROOT / "data")
