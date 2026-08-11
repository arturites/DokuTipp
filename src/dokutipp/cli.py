"""Command-line orchestration for DokuTipp."""

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from .parser import DEFAULT_CHANNELS

FILMLISTE_FILENAME = "Filmliste-akt.xz"
DOWNLOAD_URL = "https://liste.mediathekview.de/Filmliste-akt.xz"
MAX_AGE_SECONDS = 24 * 3600
DEFAULT_LIMIT = 1337
DEFAULT_MIN_DURATION = 42


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")


def default_data_dir() -> Path:
    """Return the legacy checkout cache, or a local CLI cache when installed."""
    package_file = Path(__file__).resolve()
    checkout_root = package_file.parents[2]
    if (checkout_root / "pyproject.toml").is_file() and (
        checkout_root / "src" / "dokutipp"
    ).is_dir():
        return checkout_root / "data"
    return Path.cwd() / "data"


def needs_download(filmliste: Path) -> bool:
    if not filmliste.exists():
        return True
    return time.time() - filmliste.stat().st_mtime > MAX_AGE_SECONDS


def ensure_filmliste(data_dir: Path) -> Path:
    """Create or refresh the existing local MediathekView cache."""
    data_dir.mkdir(parents=True, exist_ok=True)
    filmliste = data_dir / FILMLISTE_FILENAME

    if needs_download(filmliste):
        log("Downloading Filmliste-akt.xz ...")
        result = subprocess.run(["curl", "-fsSL", "-o", str(filmliste), DOWNLOAD_URL])
        if result.returncode != 0:
            print("Error: Download of Filmliste-akt.xz failed.", file=sys.stderr)
            raise SystemExit(1)
        log("Download complete.")
    else:
        log("Filmliste-akt.xz is fresh, skipping download.")

    return filmliste


def build_parser_command(
    filmliste: Path,
    *,
    limit: int,
    min_duration: int,
    channels: Sequence[str],
) -> list:
    """Build the legacy parser subprocess invocation."""
    command = [
        sys.executable,
        str(Path(__file__).with_name("parser.py")),
        str(filmliste),
        "--limit",
        str(limit),
        "--min-duration",
        str(min_duration),
    ]
    if tuple(channels) != DEFAULT_CHANNELS:
        command.extend(["--channels", *channels])
    return command


def run_curation(
    *,
    data_dir: Optional[Path] = None,
    limit: int = DEFAULT_LIMIT,
    min_duration: int = DEFAULT_MIN_DURATION,
    channels: Sequence[str] = DEFAULT_CHANNELS,
) -> None:
    """Run the legacy download-and-filter workflow."""
    if data_dir is None:
        data_dir = default_data_dir()

    filmliste = ensure_filmliste(data_dir)
    log("Starting parse_filmliste.py ...")
    subprocess.run(
        build_parser_command(
            filmliste,
            limit=limit,
            min_duration=min_duration,
            channels=channels,
        ),
        check=True,
    )


def run_default(*, data_dir: Optional[Path] = None) -> None:
    """Run the exact defaults previously hard-coded in start_curation.py."""
    run_curation(
        data_dir=data_dir,
        limit=DEFAULT_LIMIT,
        min_duration=DEFAULT_MIN_DURATION,
        channels=DEFAULT_CHANNELS,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download the MediathekView film list and output filtered documentary candidates as JSON."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        metavar="N",
        help="Maximum number of output entries (default: 1337)",
    )
    parser.add_argument(
        "--min-duration",
        type=int,
        default=DEFAULT_MIN_DURATION,
        metavar="MINUTES",
        help="Exclude entries shorter than MINUTES minutes (default: 42)",
    )
    parser.add_argument(
        "--channels",
        nargs="+",
        default=DEFAULT_CHANNELS,
        metavar="CHANNEL",
        help="Channels to include (default: ARD ZDF ARTE.DE)",
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    data_dir: Optional[Path] = None,
) -> None:
    args = build_argument_parser().parse_args(argv)
    run_curation(
        data_dir=data_dir,
        limit=args.limit,
        min_duration=args.min_duration,
        channels=args.channels,
    )
