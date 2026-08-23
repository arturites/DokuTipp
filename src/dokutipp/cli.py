"""Command-line orchestration for DokuTipp."""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, TextIO

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
    PROFILE_FILENAME,
    SKILL_DIRECTORY_NAME,
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
    parse_filmliste,
)
from .rendering import (
    render_insufficient_candidates,
    render_no_candidates,
    render_recommendations,
)
from .selection import (
    DEFAULT_CHUNK_SIZE,
    SelectionError,
    TOTAL_RECOMMENDATION_COUNT,
    build_candidate_pool,
    candidate_id,
    select_recursively,
)


HISTORY_FILENAME = "recommendation-history.json"
DEFAULT_MIN_DURATION = 42


def log(message: str) -> None:
    """Write one typed progress event away from command result stdout."""
    _write_event(
        "progress",
        timestamp=datetime.now().isoformat(timespec="seconds"),
        message=message,
    )


def _write_json_line(value: Mapping[str, Any], output: TextIO) -> None:
    """Write and flush one compact JSON object for the running CLI protocol."""
    json.dump(value, output, ensure_ascii=False, separators=(",", ":"))
    output.write("\n")
    output.flush()


def _write_event(
    event_type: str,
    *,
    output: Optional[TextIO] = None,
    **fields: Any,
) -> None:
    """Write one compact typed event to the recommendation event stream."""
    if output is None:
        output = sys.stderr
    _write_json_line({"type": event_type, **fields}, output)


def default_data_dir(home: Optional[Path] = None) -> Path:
    """Return DokuTipp's per-user cache directory."""
    return data_directory(home)


def default_history_file(data_dir: Optional[Path] = None) -> Path:
    """Return the local recommendation-history file for the active data cache."""
    if data_dir is None:
        data_dir = default_data_dir()
    return data_dir / HISTORY_FILENAME


def ensure_filmliste(data_dir: Path) -> Path:
    """Create or refresh the existing local MediathekView cache."""
    try:
        return prepare_filmliste(data_dir, log=log)
    except FilmlisteError as error:
        _write_event("error", message=str(error))
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


def load_profile(skill_root: Path) -> str:
    """Read the configured editorial profile once for one recommendation run."""
    profile_file = skill_root / SKILL_DIRECTORY_NAME / PROFILE_FILENAME
    try:
        return profile_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise OnboardingError(
            f"Could not read DokuTipp profile {profile_file}: {error}"
        ) from error


def run_recommendations(
    *,
    profile: str,
    data_dir: Optional[Path] = None,
    min_duration: int = DEFAULT_MIN_DURATION,
    excluded_channels: Sequence[str] = (),
    filter_file: Optional[Path] = None,
    input_stream: Optional[TextIO] = None,
    event_output: Optional[TextIO] = None,
    output: Optional[TextIO] = None,
    today: Optional[date] = None,
    history_file: Optional[Path] = None,
    history_now: Optional[float] = None,
    rng: Optional[random.Random] = None,
    request_id_factory: Optional[Callable[[], Any]] = None,
) -> None:
    """Run recursive ID selection and emit only the final Markdown on stdout."""
    if input_stream is None:
        input_stream = sys.stdin
    if event_output is None:
        event_output = sys.stderr
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
    def history_warning(message: str) -> None:
        _write_event("warning", output=event_output, message=message)

    recent_ids = load_recent_ids(
        default_history_file(data_dir) if history_file is None else history_file,
        now=history_now,
        warn=history_warning,
        read_only=True,
    )
    candidate_pool = build_candidate_pool(
        candidates,
        excluded_ids=recent_ids,
    )

    available = len(candidate_pool)
    if not available:
        output.write(render_no_candidates(today=today))
        return
    if available < TOTAL_RECOMMENDATION_COUNT:
        output.write(
            render_insufficient_candidates(
                available=available,
                required=TOTAL_RECOMMENDATION_COUNT,
                today=today,
            )
        )
        return

    def choose(
        request: Mapping[str, Any], validation_error: Optional[SelectionError]
    ) -> str:
        if validation_error is not None:
            _write_event(
                "selection_error",
                output=event_output,
                request_id=request.get("request_id"),
                message=str(validation_error),
            )
        try:
            _write_json_line(request, event_output)
            response = input_stream.readline()
        except (OSError, UnicodeError, ValueError) as error:
            raise SelectionError(
                f"Could not exchange a candidate selection: {error}"
            ) from error
        if response == "":
            raise SelectionError(
                "Candidate selection input ended before the workflow was complete."
            )
        return response.rstrip("\r\n")

    selection = select_recursively(
        candidate_pool,
        profile=profile,
        choose=choose,
        chunk_size=DEFAULT_CHUNK_SIZE,
        rng=rng,
        request_id_factory=request_id_factory,
    )
    rendered = render_recommendations(selection, today=today)
    selected_ids = [
        *(candidate_id(candidate) for candidate in selection.recommendations),
        candidate_id(selection.extra_recommendation),
    ]
    record_selected_ids(
        default_history_file(data_dir) if history_file is None else history_file,
        selected_ids,
        now=history_now,
        warn=history_warning,
    )
    output.write(rendered)


def add_recommendation_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the filters supported by the bare recommendation workflow."""
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


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Select and render a current 3+1 DokuTipp result."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"dokutipp {__version__}",
    )
    add_recommendation_arguments(parser)
    subparsers = parser.add_subparsers(dest="command")

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
    legacy_invocation = bool(arguments) and arguments[0] in {"fetch", "select"}
    # Parse every current interface before preflight so help, version, setup,
    # and invalid arguments cannot mutate local state. Only the two retired
    # command forms reach preflight first, allowing their canonical old skill
    # to be upgraded before argparse reports the breaking interface change.
    args = None if legacy_invocation else parser.parse_args(arguments)
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

    if args is not None and args.command == "setup":
        try:
            run_setup(**onboarding_options)
        except OnboardingError as error:
            print(f"Error: {error}", file=sys.stderr)
            raise SystemExit(2)
        except KeyboardInterrupt:
            print("Cancelled.", file=sys.stderr)
            raise SystemExit(130)
        return

    try:
        setup_just_ran = ensure_installation(**onboarding_options)
    except OnboardingError as error:
        _write_event("error", message=str(error))
        raise SystemExit(2)
    except KeyboardInterrupt:
        _write_event("error", message="Recommendation workflow cancelled.")
        raise SystemExit(130)

    if setup_just_ran:
        return

    if args is None:
        args = parser.parse_args(arguments)

    try:
        app_config = load_config(effective_config_file)
        if app_config is None:
            raise OnboardingError(
                f"DokuTipp config is missing after setup: {effective_config_file}"
            )
        excluded_channels = load_sender_filters(app_config.sender_filter_file)
        profile = load_profile(app_config.skill_root)
        run_recommendations(
            profile=profile,
            data_dir=effective_data_dir,
            min_duration=args.min_duration,
            excluded_channels=excluded_channels,
            filter_file=args.filter_file,
            input_stream=input_stream,
            history_file=history_file,
            history_now=history_now,
        )
    except (
        FilterConfigError,
        OnboardingError,
        RecommendationHistoryError,
        SelectionError,
    ) as error:
        _write_event("error", message=str(error))
        raise SystemExit(2)
    except KeyboardInterrupt:
        _write_event("error", message="Recommendation workflow cancelled.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
