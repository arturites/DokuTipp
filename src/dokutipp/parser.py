"""Parse and filter MediathekView film lists."""

import argparse
import json
import lzma
import re
import sys
import sysconfig
import time
from pathlib import Path
from typing import Optional, Sequence, TextIO, Tuple, Union

# Field indices in each "X" array entry
IDX_SENDER = 0
IDX_THEMA = 1
IDX_TITEL = 2
IDX_DATUM = 3       # "DD.MM.YYYY" string
IDX_DAUER = 5       # "HH:MM:SS" string
IDX_BESCHREIBUNG = 7
IDX_WEBSITE = 9     # Mediathek page URL
IDX_DATUM_L = 16    # Unix timestamp as string

SEVEN_DAYS = 7 * 24 * 3600
DEFAULT_CHANNELS: Tuple[str, ...] = ("ARD", "ZDF", "ARTE.DE")
FILTER_FILENAME = "filters.txt"


class FilterConfigError(ValueError):
    """Raised when the title filter configuration cannot be used."""


def default_filter_file() -> Path:
    """Return the source-checkout or installed default title filter file."""
    package_file = Path(__file__).resolve()
    checkout_root = package_file.parents[2]
    checkout_file = checkout_root / FILTER_FILENAME
    if (checkout_root / "pyproject.toml").is_file() and (
        checkout_root / "src" / "dokutipp"
    ).is_dir():
        return checkout_file

    working_directory_file = Path.cwd() / FILTER_FILENAME
    if working_directory_file.is_file():
        return working_directory_file

    installed_file = Path(sysconfig.get_path("data")) / FILTER_FILENAME
    if installed_file.is_file():
        return installed_file

    return working_directory_file


def load_title_filters(
    filter_file: Optional[Union[str, Path]] = None,
) -> Tuple[str, ...]:
    """Read and validate case-insensitive title exclusion regexes."""
    path = Path(filter_file) if filter_file is not None else default_filter_file()
    if not path.is_file():
        raise FilterConfigError(f"Title filter file not found: {path}")

    patterns = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise FilterConfigError(
            f"Could not read title filter file {path}: {error}"
        ) from error

    for line_number, line in enumerate(lines, start=1):
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as error:
            raise FilterConfigError(
                f"Invalid title filter in {path} on line {line_number}: {error}"
            ) from error
        patterns.append(pattern)

    return tuple(patterns)


def parse_raw(data: str) -> list:
    """Extract all (key, value) pairs from the non-standard duplicate-key JSON.

    Uses json.JSONDecoder.raw_decode() to correctly handle all JSON escaping
    and avoids fragile regex over potentially large string values.
    """
    decoder = json.JSONDecoder()
    pairs = []

    start = data.find("{")
    if start == -1:
        return pairs
    pos = start + 1

    while pos < len(data):
        # Skip whitespace and commas between pairs
        while pos < len(data) and data[pos] in " \t\n\r,":
            pos += 1

        if pos >= len(data) or data[pos] == "}":
            break

        # Expect a quoted key
        if data[pos] != '"':
            break

        # Find end of key (unescaped closing quote)
        key_end = pos + 1
        while key_end < len(data):
            if data[key_end] == "\\":
                key_end += 2  # skip escaped character
                continue
            if data[key_end] == '"':
                break
            key_end += 1

        key = data[pos + 1:key_end]
        pos = key_end + 1

        # Skip whitespace and the colon
        while pos < len(data) and data[pos] in " \t\n\r:":
            pos += 1

        # Parse the value with the standard JSON decoder
        try:
            value, end = decoder.raw_decode(data, pos)
        except json.JSONDecodeError:
            break

        pairs.append((key, value))
        pos = end

    return pairs


def parse_filmliste(
    file_path: Union[str, Path],
    *,
    limit: Optional[int] = None,
    min_duration: int = 0,
    channels: Sequence[str] = DEFAULT_CHANNELS,
    filter_file: Optional[Union[str, Path]] = None,
) -> list:
    """Return the existing filtered candidate list from *file_path*."""
    allowed_channels = set(channels)
    title_filters = tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in load_title_filters(filter_file)
    )
    now = time.time()
    cutoff = now - SEVEN_DAYS

    with lzma.open(file_path, "rt", encoding="utf-8") as file_handle:
        data = file_handle.read()

    pairs = parse_raw(data)

    last_sender = ""
    last_thema = ""
    results = []

    for key, value in pairs:
        if key != "X":
            continue

        # Delta-encoding: empty Sender/Thema inherit from previous entry.
        # Must be resolved before filtering so the chain is never broken.
        sender = value[IDX_SENDER] if value[IDX_SENDER] else last_sender
        thema = value[IDX_THEMA] if value[IDX_THEMA] else last_thema
        last_sender = sender
        last_thema = thema

        if sender not in allowed_channels:
            continue

        try:
            datum_l = int(value[IDX_DATUM_L])
        except (ValueError, IndexError):
            continue

        if datum_l < cutoff or datum_l > now:
            continue

        if any(pattern.search(value[IDX_TITEL]) for pattern in title_filters):
            continue

        if min_duration > 0:
            try:
                h, m, _s = value[IDX_DAUER].split(":")
                if int(h) * 60 + int(m) < min_duration:
                    continue
            except (ValueError, AttributeError):
                continue

        results.append(
            {
                "title": value[IDX_TITEL],
                "channel": sender,
                "date": value[IDX_DATUM],
                "duration": value[IDX_DAUER],
                "description": value[IDX_BESCHREIBUNG],
                "website": value[IDX_WEBSITE],
            }
        )

        if limit is not None and len(results) >= limit:
            break

    return results


def write_results(results: list, output: Optional[TextIO] = None) -> None:
    """Write parser results using the legacy JSON format."""
    if output is None:
        output = sys.stdout
    json.dump(results, output, ensure_ascii=False, indent=2)
    output.write("\n")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse MediathekView Filmliste-akt.xz and output filtered JSON."
    )
    parser.add_argument("file", help="Path to Filmliste-akt.xz")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Maximum number of output entries (default: no limit)",
    )
    parser.add_argument(
        "--min-duration",
        type=int,
        default=0,
        metavar="MINUTES",
        help="Exclude entries shorter than MINUTES minutes (default: 0)",
    )
    parser.add_argument(
        "--channels",
        nargs="+",
        default=DEFAULT_CHANNELS,
        metavar="CHANNEL",
        help="Channels to include (default: ARD ZDF ARTE.DE)",
    )
    parser.add_argument(
        "--filter-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Title exclusion regex file (default: filters.txt)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = build_argument_parser().parse_args(argv)
    try:
        results = parse_filmliste(
            args.file,
            limit=args.limit,
            min_duration=args.min_duration,
            channels=args.channels,
            filter_file=args.filter_file,
        )
    except FilterConfigError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2)
    write_results(results)


if __name__ == "__main__":
    main()
