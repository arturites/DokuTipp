#!/usr/bin/env python3
"""Legacy compatibility wrapper for the parser CLI."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from dokutipp.parser import (
    DEFAULT_CHANNELS,
    IDX_BESCHREIBUNG,
    IDX_DATUM,
    IDX_DATUM_L,
    IDX_DAUER,
    IDX_SENDER,
    IDX_THEMA,
    IDX_TITEL,
    IDX_WEBSITE,
    SEVEN_DAYS,
    build_argument_parser,
    main,
    parse_filmliste,
    parse_raw,
    write_results,
)


if __name__ == "__main__":
    main()
