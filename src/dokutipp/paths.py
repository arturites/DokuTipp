"""Resolve DokuTipp's user-specific configuration and data paths."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


APP_DIRECTORY_NAME = ".dokutipp"
CONFIG_FILENAME = "config.json"
DATA_DIRECTORY_NAME = "data"
SENDER_FILTER_FILENAME = "senders.txt"


def app_directory(home: Optional[Path] = None) -> Path:
    """Return DokuTipp's per-user application directory."""
    if home is None:
        home = Path.home()
    return home / APP_DIRECTORY_NAME


def config_file(home: Optional[Path] = None) -> Path:
    """Return DokuTipp's per-user onboarding configuration file."""
    return app_directory(home) / CONFIG_FILENAME


def data_directory(home: Optional[Path] = None) -> Path:
    """Return DokuTipp's per-user cache and history directory."""
    return app_directory(home) / DATA_DIRECTORY_NAME


def sender_filter_file(home: Optional[Path] = None) -> Path:
    """Return the default personal broadcaster-exclusion file."""
    return app_directory(home) / SENDER_FILTER_FILENAME
