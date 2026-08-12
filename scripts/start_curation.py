#!/usr/bin/env python3
"""Compatibility wrapper for DokuTipp's candidate-fetch workflow."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from dokutipp.cli import main


if __name__ == "__main__":
    main(["fetch", *sys.argv[1:]], data_dir=PROJECT_ROOT / "data")
