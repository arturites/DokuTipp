"""Command-line orchestration for DokuTipp."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, TextIO

from .onboarding import OnboardingError, ensure_installation, run_setup
from .parser import (
    DEFAULT_CHANNELS,
    FilterConfigError,
    load_title_filters,
    parse_filmliste,
)
from .rendering import (
    render_insufficient_candidates,
    render_no_candidates,
    render_recommendations,
)
from .selection import (
    SelectionError,
    TOTAL_RECOMMENDATION_COUNT,
    build_fetch_payload,
    resolve_selection,
)


FILMLISTE_FILENAME = "Filmliste-akt.xz"
DOWNLOAD_URL = "https://liste.mediathekview.de/Filmliste-akt.xz"
MAX_AGE_SECONDS = 24 * 3600
DEFAULT_LIMIT: Optional[int] = None
DEFAULT_MIN_DURATION = 42


def log(message: str) -> None:
    """Write operational progress away from command result stdout."""
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}",
        file=sys.stderr,
    )


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


def load_candidates(
    *,
    data_dir: Optional[Path] = None,
    limit: Optional[int] = DEFAULT_LIMIT,
    min_duration: int = DEFAULT_MIN_DURATION,
    channels: Sequence[str] = DEFAULT_CHANNELS,
    filter_file: Optional[Path] = None,
) -> list:
    """Load the cache and reuse the existing MediathekView parser filters."""
    if data_dir is None:
        data_dir = default_data_dir()
    filmliste = ensure_filmliste(data_dir)
    return parse_filmliste(
        filmliste,
        limit=limit,
        min_duration=min_duration,
        channels=channels,
        filter_file=filter_file,
    )


def run_fetch(
    *,
    data_dir: Optional[Path] = None,
    limit: Optional[int] = DEFAULT_LIMIT,
    min_duration: int = DEFAULT_MIN_DURATION,
    channels: Sequence[str] = DEFAULT_CHANNELS,
    filter_file: Optional[Path] = None,
    output: Optional[TextIO] = None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Write the structured candidate set for agent-side ID selection."""
    if output is None:
        output = sys.stdout
    if today is None:
        today = date.today()

    candidates = load_candidates(
        data_dir=data_dir,
        limit=limit,
        min_duration=min_duration,
        channels=channels,
        filter_file=filter_file,
    )
    title_filters = load_title_filters(filter_file)
    payload = build_fetch_payload(
        candidates,
        limit=limit,
        min_duration=min_duration,
        channels=channels,
        title_filters=title_filters,
    )
    if payload["status"] == "no_candidates":
        payload["message"] = render_no_candidates(today=today)
    elif payload["status"] == "insufficient_candidates":
        payload["message"] = render_insufficient_candidates(
            available=len(payload["candidates"]),
            required=TOTAL_RECOMMENDATION_COUNT,
            today=today,
        )
    json.dump(payload, output, ensure_ascii=False, indent=2)
    output.write("\n")
    return payload


def run_select(
    selection_argument: str,
    *,
    data_dir: Optional[Path] = None,
    limit: Optional[int] = DEFAULT_LIMIT,
    min_duration: int = DEFAULT_MIN_DURATION,
    channels: Sequence[str] = DEFAULT_CHANNELS,
    filter_file: Optional[Path] = None,
    output: Optional[TextIO] = None,
    today: Optional[date] = None,
) -> None:
    """Resolve an agent selection and write the complete final Markdown."""
    if output is None:
        output = sys.stdout
    if today is None:
        today = date.today()

    candidates = load_candidates(
        data_dir=data_dir,
        limit=limit,
        min_duration=min_duration,
        channels=channels,
        filter_file=filter_file,
    )
    selection = resolve_selection(selection_argument, candidates)
    output.write(render_recommendations(selection, today=today))


def add_filter_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the shared MediathekView filter options to one subcommand."""
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        metavar="N",
        help="Maximum number of filtered source candidates (default: no limit)",
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
        help="Channels to include (default: all channels)",
    )
    parser.add_argument(
        "--filter-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Title exclusion regex file (default: filters.txt)",
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch MediathekView candidates or render a selected 3+1 DokuTipp result."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Output filtered candidates with stable IDs as JSON.",
    )
    add_filter_arguments(fetch_parser)

    select_parser = subparsers.add_parser(
        "select",
        help="Validate selected IDs and output the final recommendations.",
    )
    select_parser.add_argument(
        "ids",
        metavar="IDS",
        help=(
            "Four comma-separated SHA-256 IDs; prefix exactly one extra "
            "recommendation with lowercase 'x'."
        ),
    )
    add_filter_arguments(select_parser)

    subparsers.add_parser(
        "setup",
        help="Interactively configure the DokuTipp skill installation.",
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    data_dir: Optional[Path] = None,
    config_file: Optional[Path] = None,
    input_stream: Optional[TextIO] = None,
    onboarding_output: Optional[TextIO] = None,
    canonical_skill_file: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> None:
    parser = build_argument_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)

    onboarding_options = {
        "config_file": config_file,
        "input_stream": input_stream,
        "output_stream": onboarding_output,
        "canonical_skill_file": canonical_skill_file,
        "environment": environment,
        "home": home,
    }

    if arguments == ["setup"]:
        try:
            run_setup(**onboarding_options)
        except OnboardingError as error:
            print(f"Error: {error}", file=sys.stderr)
            raise SystemExit(2)
        return

    try:
        setup_just_ran = ensure_installation(**onboarding_options)
    except OnboardingError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2)

    if not arguments:
        if setup_just_ran:
            return
        parser.print_help(file=sys.stderr)
        raise SystemExit(2)

    args = parser.parse_args(arguments)
    try:
        if args.command == "fetch":
            run_fetch(
                data_dir=data_dir,
                limit=args.limit,
                min_duration=args.min_duration,
                channels=args.channels,
                filter_file=args.filter_file,
            )
        elif args.command == "select":
            run_select(
                args.ids,
                data_dir=data_dir,
                limit=args.limit,
                min_duration=args.min_duration,
                channels=args.channels,
                filter_file=args.filter_file,
            )
        elif args.command == "setup":
            # The exact `dokutipp setup` form was handled before preflight so it
            # can deliberately reconfigure an existing installation.
            parser.print_help(file=sys.stderr)
            raise SystemExit(2)
        else:
            parser.print_help(file=sys.stderr)
            raise SystemExit(2)
    except (FilterConfigError, SelectionError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
