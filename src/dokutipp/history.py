"""Local, short-lived history for selected DokuTipp recommendations."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Collection, Dict, Iterator, Optional, Set

try:  # DokuTipp supports macOS and Linux, where advisory file locks are present.
    import fcntl
except ImportError:  # pragma: no cover - defensive fallback for unsupported systems
    fcntl = None  # type: ignore[assignment]

from .selection import CANDIDATE_ID_PATTERN


HISTORY_VERSION = 1
HISTORY_TTL_SECONDS = 7 * 24 * 60 * 60


class RecommendationHistoryError(RuntimeError):
    """Raised when DokuTipp cannot maintain its recommendation history."""


WarningCallback = Callable[[str], None]


def load_recent_ids(
    history_file: Path,
    *,
    now: Optional[float] = None,
    warn: Optional[WarningCallback] = None,
    read_only: bool = False,
) -> Set[str]:
    """Return selected IDs younger than seven days and prune expired entries.

    Missing history never creates state. ``read_only`` also defers pruning or
    repairing an existing file until a successful selection is recorded.
    """
    timestamp = _timestamp(now)
    try:
        history_file.lstat()
    except FileNotFoundError:
        return set()
    except OSError:
        # Let the locked read below attempt the documented reset or report a
        # clear write/lock error instead of silently treating state as missing.
        pass

    with _exclusive_lock(history_file):
        entries, _ = _read_entries(
            history_file,
            warn=warn,
            reset_invalid=not read_only,
        )
        recent_entries = _recent_entries(entries, now=timestamp)
        if not read_only and recent_entries != entries:
            _write_entries(history_file, recent_entries)
        return set(recent_entries)


def record_selected_ids(
    history_file: Path,
    identifiers: Collection[str],
    *,
    now: Optional[float] = None,
    warn: Optional[WarningCallback] = None,
) -> None:
    """Remember selected SHA-256 IDs, replacing their timestamps if repeated."""
    if not identifiers:
        return
    invalid_identifier = next(
        (
            identifier
            for identifier in identifiers
            if not isinstance(identifier, str)
            or not CANDIDATE_ID_PATTERN.fullmatch(identifier)
        ),
        None,
    )
    if invalid_identifier is not None:
        raise RecommendationHistoryError(
            "Recommendation history can only store complete lowercase SHA-256 IDs."
        )

    timestamp = _timestamp(now)
    with _exclusive_lock(history_file):
        entries, _reset = _read_entries(
            history_file,
            warn=warn,
            reset_invalid=False,
        )
        entries = _recent_entries(entries, now=timestamp)
        entries.update({identifier: timestamp for identifier in identifiers})
        _write_entries(history_file, entries)


@contextmanager
def _exclusive_lock(history_file: Path) -> Iterator[None]:
    """Hold an advisory lock while reading or replacing one history file."""
    lock_file = history_file.with_name(f"{history_file.name}.lock")
    try:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        with lock_file.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as error:
        raise RecommendationHistoryError(
            f"Could not lock recommendation history {history_file}: {error}"
        ) from error


def _read_entries(
    history_file: Path,
    *,
    warn: Optional[WarningCallback],
    reset_invalid: bool = True,
) -> tuple[Dict[str, float], bool]:
    """Load valid entries, or atomically reset a malformed history file."""
    try:
        if history_file.is_symlink():
            raise ValueError("history file must not be a symbolic link")
        value = json.loads(history_file.read_text(encoding="utf-8"))
        return _validate_entries(value), False
    except FileNotFoundError:
        return {}, False
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError) as error:
        if reset_invalid:
            _write_entries(history_file, {})
        _warn(
            warn,
            f"Recommendation history {history_file} was invalid and "
            f"{'has been reset' if reset_invalid else 'will be reset after a successful selection'} "
            f"({error}).",
        )
        return {}, True


def _validate_entries(value: object) -> Dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError("unsupported history format")
    version = value.get("version")
    if type(version) is not int or version != HISTORY_VERSION:
        raise ValueError("unsupported history format")
    selected_at = value.get("selected_at")
    if not isinstance(selected_at, dict):
        raise ValueError("history selected_at must be an object")

    entries: Dict[str, float] = {}
    for identifier, selected_time in selected_at.items():
        if not isinstance(identifier, str) or not CANDIDATE_ID_PATTERN.fullmatch(identifier):
            raise ValueError("history contains an invalid candidate ID")
        if (
            isinstance(selected_time, bool)
            or not isinstance(selected_time, (int, float))
            or not math.isfinite(selected_time)
        ):
            raise ValueError("history contains an invalid timestamp")
        entries[identifier] = float(selected_time)
    return entries


def _recent_entries(entries: Dict[str, float], *, now: float) -> Dict[str, float]:
    return {
        identifier: selected_time
        for identifier, selected_time in entries.items()
        if 0 <= now - selected_time < HISTORY_TTL_SECONDS
    }


def _write_entries(history_file: Path, entries: Dict[str, float]) -> None:
    """Atomically replace the on-disk document while retaining a valid old file."""
    temporary_name: Optional[str] = None
    try:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(history_file.parent),
            prefix=f".{history_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_name = temporary_file.name
            json.dump(
                {
                    "version": HISTORY_VERSION,
                    "selected_at": entries,
                },
                temporary_file,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, history_file)
    except OSError as error:
        raise RecommendationHistoryError(
            f"Could not write recommendation history {history_file}: {error}"
        ) from error
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _timestamp(now: Optional[float]) -> float:
    value = time.time() if now is None else now
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise RecommendationHistoryError("Recommendation history needs a finite timestamp.")
    return float(value)


def _warn(warn: Optional[WarningCallback], message: str) -> None:
    if warn is not None:
        warn(message)
