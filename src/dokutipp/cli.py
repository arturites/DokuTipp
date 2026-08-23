"""Command-line orchestration for DokuTipp."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, TextIO

from . import __version__
from .history import (
    RecommendationHistoryError,
    load_recent_ids,
    record_selected_ids,
)
from .filmliste import (
    DOWNLOAD_URL,
    FILMLISTE_FILENAME,
    MAX_AGE_SECONDS,
    FilmlisteError,
    ensure_filmliste as prepare_filmliste,
    needs_download,
)
from .onboarding import (
    OnboardingError,
    SenderSelector,
    config_path,
    ensure_installation,
    load_config,
    run_setup,
)
from .paths import data_directory
from .parser import (
    FilterConfigError,
    load_sender_filters,
    load_title_filters,
    parse_filmliste,
)
from .rendering import (
    render_insufficient_candidates,
    render_no_candidates,
    render_recommendations,
)
from .selection import (
    PaginationError,
    SelectionError,
    TOTAL_RECOMMENDATION_COUNT,
    build_candidate_pool,
    build_fetch_payload,
    candidate_id,
    parse_selection_argument,
    paginate_candidates,
    resolve_selection,
)


HISTORY_FILENAME = "recommendation-history.json"
DEFAULT_LIMIT = 50
DEFAULT_PAGE = 1
DEFAULT_MIN_DURATION = 42


def log(message: str) -> None:
    """Write operational progress away from command result stdout."""
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}",
        file=sys.stderr,
    )


def default_data_dir(home: Optional[Path] = None) -> Path:
    """Return DokuTipp's per-user cache directory."""
    return data_directory(home)


def default_history_file(data_dir: Optional[Path] = None) -> Path:
    """Return the local recommendation-history file for the active data cache."""
    if data_dir is None:
        data_dir = default_data_dir()
    return data_dir / HISTORY_FILENAME


def _history_warning(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def ensure_filmliste(data_dir: Path) -> Path:
    """Create or refresh the existing local MediathekView cache."""
    try:
        return prepare_filmliste(data_dir, log=log)
    except FilmlisteError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)


def load_candidates(
    *,
    data_dir: Optional[Path] = None,
    min_duration: int = DEFAULT_MIN_DURATION,
    excluded_channels: Sequence[str] = (),
    filter_file: Optional[Path] = None,
) -> list:
    """Load the cache and reuse the existing MediathekView parser filters."""
    if data_dir is None:
        data_dir = default_data_dir()
    filmliste = ensure_filmliste(data_dir)
    return parse_filmliste(
        filmliste,
        min_duration=min_duration,
        excluded_channels=excluded_channels,
        filter_file=filter_file,
    )


def run_fetch(
    *,
    data_dir: Optional[Path] = None,
    limit: int = DEFAULT_LIMIT,
    page: int = DEFAULT_PAGE,
    min_duration: int = DEFAULT_MIN_DURATION,
    excluded_channels: Sequence[str] = (),
    filter_file: Optional[Path] = None,
    output: Optional[TextIO] = None,
    today: Optional[date] = None,
    history_file: Optional[Path] = None,
    history_now: Optional[float] = None,
) -> Dict[str, Any]:
    """Write the structured candidate set for agent-side ID selection."""
    if output is None:
        output = sys.stdout
    if today is None:
        today = date.today()

    candidates = load_candidates(
        data_dir=data_dir,
        min_duration=min_duration,
        excluded_channels=excluded_channels,
        filter_file=filter_file,
    )
    title_filters = load_title_filters(filter_file)
    recent_ids = load_recent_ids(
        default_history_file(data_dir) if history_file is None else history_file,
        now=history_now,
        warn=_history_warning,
    )
    payload = build_fetch_payload(
        candidates,
        limit=limit,
        page=page,
        min_duration=min_duration,
        excluded_channels=excluded_channels,
        title_filters=title_filters,
        excluded_ids=recent_ids,
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
    limit: int = DEFAULT_LIMIT,
    page: int = DEFAULT_PAGE,
    min_duration: int = DEFAULT_MIN_DURATION,
    excluded_channels: Sequence[str] = (),
    filter_file: Optional[Path] = None,
    output: Optional[TextIO] = None,
    today: Optional[date] = None,
    history_file: Optional[Path] = None,
    history_now: Optional[float] = None,
) -> None:
    """Resolve an agent selection and write the complete final Markdown."""
    if output is None:
        output = sys.stdout
    if today is None:
        today = date.today()

    candidates = load_candidates(
        data_dir=data_dir,
        min_duration=min_duration,
        excluded_channels=excluded_channels,
        filter_file=filter_file,
    )
    recent_ids = load_recent_ids(
        default_history_file(data_dir) if history_file is None else history_file,
        now=history_now,
        warn=_history_warning,
    )
    candidate_pool = build_candidate_pool(candidates, excluded_ids=recent_ids)
    candidate_page = paginate_candidates(candidate_pool, limit=limit, page=page)
    browsed_candidates = tuple(
        sorted(candidate_pool, key=candidate_id)[: candidate_page.end]
    )
    if len(browsed_candidates) < TOTAL_RECOMMENDATION_COUNT:
        raise SelectionError(
            "The browsed pages contain fewer than four selectable candidates."
        )
    selection = resolve_selection(selection_argument, browsed_candidates)
    rendered = render_recommendations(selection, today=today)
    recommendation_ids, extra_recommendation_id = parse_selection_argument(
        selection_argument
    )
    record_selected_ids(
        default_history_file(data_dir) if history_file is None else history_file,
        [*recommendation_ids, extra_recommendation_id],
        now=history_now,
        warn=_history_warning,
    )
    output.write(rendered)


def add_filter_arguments(parser: argparse.ArgumentParser) -> None:
    """Add shared filter and pagination options to one subcommand."""
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=DEFAULT_LIMIT,
        metavar="N",
        help="Number of candidates shown per page (default: 50)",
    )
    parser.add_argument(
        "--page",
        type=_positive_int,
        default=DEFAULT_PAGE,
        metavar="N",
        help="One-based candidate page to show (default: 1)",
    )
    parser.add_argument(
        "--min-duration",
        type=int,
        default=DEFAULT_MIN_DURATION,
        metavar="MINUTES",
        help="Exclude entries shorter than MINUTES minutes (default: 42)",
    )
    parser.add_argument(
        "--filter-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Title exclusion regex file (default: filters.txt)",
    )


def _positive_int(value: str) -> int:
    """Parse a strictly positive command-line integer."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _write_pagination_error(error: PaginationError) -> None:
    """Write one machine-readable pagination error to stderr."""
    json.dump(
        {
            "type": "error",
            "error_code": "page_out_of_range",
            "page": error.page,
            "total_pages": error.total_pages,
            "limit": error.limit,
            "total_candidates": error.total_candidates,
            "message": str(error),
        },
        sys.stderr,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    sys.stderr.write("\n")
    sys.stderr.flush()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch MediathekView candidates or render a selected 3+1 DokuTipp result."
        )
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"dokutipp {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Output filtered, not-recently-recommended candidates as JSON.",
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
    history_file: Optional[Path] = None,
    history_now: Optional[float] = None,
    sender_selector: Optional[SenderSelector] = None,
) -> None:
    parser = build_argument_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--version"]:
        parser.parse_args(arguments)
        return
    effective_data_dir = (
        data_dir if data_dir is not None else default_data_dir(home)
    )
    effective_config_file = (
        config_file
        if config_file is not None
        else config_path(environment=environment, home=home)
    )

    onboarding_options = {
        "config_file": effective_config_file,
        "data_dir": effective_data_dir,
        "input_stream": input_stream,
        "output_stream": onboarding_output,
        "canonical_skill_file": canonical_skill_file,
        "environment": environment,
        "home": home,
        "sender_selector": sender_selector,
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
        app_config = load_config(effective_config_file)
        if app_config is None:
            raise OnboardingError(
                f"DokuTipp config is missing after setup: {effective_config_file}"
            )
        excluded_channels = load_sender_filters(app_config.sender_filter_file)
        if args.command == "fetch":
            run_fetch(
                data_dir=effective_data_dir,
                limit=args.limit,
                page=args.page,
                min_duration=args.min_duration,
                excluded_channels=excluded_channels,
                filter_file=args.filter_file,
                history_file=history_file,
                history_now=history_now,
            )
        elif args.command == "select":
            run_select(
                args.ids,
                data_dir=effective_data_dir,
                limit=args.limit,
                page=args.page,
                min_duration=args.min_duration,
                excluded_channels=excluded_channels,
                filter_file=args.filter_file,
                history_file=history_file,
                history_now=history_now,
            )
        elif args.command == "setup":
            # The exact `dokutipp setup` form was handled before preflight so it
            # can deliberately reconfigure an existing installation.
            parser.print_help(file=sys.stderr)
            raise SystemExit(2)
        else:
            parser.print_help(file=sys.stderr)
            raise SystemExit(2)
    except PaginationError as error:
        _write_pagination_error(error)
        raise SystemExit(2)
    except (
        FilterConfigError,
        OnboardingError,
        RecommendationHistoryError,
        SelectionError,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
