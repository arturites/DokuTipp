"""Download and validate the shared MediathekView film-list cache."""

from __future__ import annotations

import lzma
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional


FILMLISTE_FILENAME = "Filmliste-akt.xz"
DOWNLOAD_URL = "https://liste.mediathekview.de/Filmliste-akt.xz"
MAX_AGE_SECONDS = 24 * 3600


class FilmlisteError(RuntimeError):
    """Raised when no usable MediathekView cache can be prepared."""


def needs_download(filmliste: Path, *, now: Optional[float] = None) -> bool:
    """Return whether *filmliste* is missing or older than the cache horizon."""
    if not filmliste.exists():
        return True
    if now is None:
        now = time.time()
    return now - filmliste.stat().st_mtime > MAX_AGE_SECONDS


def is_readable_filmliste(filmliste: Path) -> bool:
    """Return whether the complete XZ stream can be decompressed."""
    if not filmliste.is_file():
        return False
    try:
        with lzma.open(filmliste, "rb") as file_handle:
            while file_handle.read(1024 * 1024):
                pass
    except (OSError, EOFError, lzma.LZMAError):
        return False
    return True


def ensure_filmliste(
    data_dir: Path,
    *,
    allow_stale: bool = False,
    validate_existing: bool = False,
    log: Optional[Callable[[str], None]] = None,
    validator: Optional[Callable[[Path], bool]] = None,
) -> Path:
    """Return a current cache, downloading atomically when necessary.

    When ``allow_stale`` is true, a readable existing cache remains available
    if refreshing it fails. Downloads are validated before replacing that
    cache so a failed or corrupt transfer cannot destroy the fallback.
    """
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise FilmlisteError(
            f"Could not create MediathekView cache directory {data_dir}: {error}"
        ) from error

    filmliste = data_dir / FILMLISTE_FILENAME
    is_usable = is_readable_filmliste if validator is None else validator
    refresh_required = needs_download(filmliste)
    existing_readable: Optional[bool] = None
    if not refresh_required and validate_existing:
        existing_readable = is_usable(filmliste)
        refresh_required = not existing_readable

    if not refresh_required:
        if log is not None:
            log("Filmliste-akt.xz is fresh, skipping download.")
        return filmliste

    if allow_stale and existing_readable is None:
        existing_readable = is_usable(filmliste)

    if log is not None:
        log("Downloading Filmliste-akt.xz ...")

    temporary_path: Optional[Path] = None
    try:
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{FILMLISTE_FILENAME}.",
                suffix=".tmp",
                dir=data_dir,
            )
            os.close(descriptor)
            temporary_path = Path(temporary_name)
        except OSError as error:
            raise FilmlisteError(
                f"Could not create a temporary MediathekView cache file: {error}"
            ) from error

        try:
            result = subprocess.run(
                ["curl", "-fsSL", "-o", str(temporary_path), DOWNLOAD_URL],
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise FilmlisteError(
                f"Could not run curl to download Filmliste-akt.xz: {error}"
            ) from error
        if result.returncode != 0 or not is_usable(temporary_path):
            raise FilmlisteError("Download of Filmliste-akt.xz failed.")

        try:
            os.replace(temporary_path, filmliste)
        except OSError as error:
            raise FilmlisteError(
                f"Could not install the downloaded Filmliste-akt.xz: {error}"
            ) from error
        temporary_path = None
    except FilmlisteError:
        if allow_stale and existing_readable:
            if log is not None:
                log("Warning: download failed; using the existing film-list cache.")
            return filmliste
        raise
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    if log is not None:
        log("Download complete.")
    return filmliste
