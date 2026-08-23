"""Install and maintain DokuTipp's local agent skill."""

from __future__ import annotations

import hashlib
import json
import lzma
import os
import sysconfig
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, TextIO, Tuple

from .filmliste import FilmlisteError, ensure_filmliste
from .parser import FilterConfigError, available_channels, load_sender_filters
from .paths import (
    config_file as default_config_file,
    data_directory,
    sender_filter_file as default_sender_filter_file,
)


PROFILE_FILENAME = "PROFILE.md"
SKILL_DIRECTORY_NAME = "dokutipp"
SKILL_FILENAME = "SKILL.md"
KNOWN_CANONICAL_SKILL_SHA256S = frozenset(
    {
        # Released canonical skills from v1.0.0 through v2.1.0. Exact hashes
        # distinguish untouched bundled files from user-edited installations.
        "7bfd3fedb222cc5301b3913d907153cd3df236e810227c3050c4472ce1565efa",
        "2a254d0cd38012fe0eee42a7bed6cdda2fe542847bf35370daf107f4e2b82b33",
        "f88882d48add9a507cc0a9ac1f9c3fee5ba76eeb53835875a693f8809ecee887",
        "8f9e570d44a119ffd0ec57da910c3fbe8e4c3854d2276cafea247e71aa0c21c7",
        "6497692638dab8b0512e39ee40886d436e638c01492a6dc8dc6abb74ba1ad97b",
        "66b25eac1a94dee212b0d68adbd799cb1724c92c1b3d412fe4a407766a23828e",
        "2f8cc3148a80bbd132a51dc8f732852a28ce8df702fa15209ffe99ff37524c29",
        "7f4b7bd1a23793d63182c8f83397548cc75635c0d28c00abd4110cf335fad8ad",
    }
)
INTERESTS_PROMPT = (
    "What documentary topics interest you? "
    "Enter them comma-separated on one line (for example: history, science, nature): "
)
AVOID_PROMPT = (
    "Which topics would you like to avoid? "
    "Enter them comma-separated on one line (optional): "
)


@dataclass(frozen=True)
class AppConfig:
    """Validated per-user runtime and skill configuration."""

    skill_root: Path
    sender_filter_file: Optional[Path] = None


SenderSelector = Callable[
    [Sequence[str], Sequence[str]], Optional[Sequence[str]]
]


PROFILE_TEMPLATE = """# Personal Profile

This file provides editorial context for ID-only documentary selection.

---

## Interests

{interests}

### Topics to avoid

{avoid}
"""


class OnboardingError(RuntimeError):
    """Raised when the local DokuTipp skill cannot be prepared safely."""


def config_path(
    *,
    environment: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    """Return DokuTipp's user-specific setup file location."""
    # Keep the environment argument for callers that also inject it into the
    # Hermes skill-root resolver. XDG_CONFIG_HOME is intentionally not used for
    # DokuTipp's per-user application directory.
    del environment
    return default_config_file(home)


def canonical_skill_path() -> Path:
    """Return the canonical skill from a checkout or an installed package."""
    package_file = Path(__file__).resolve()
    checkout_root = package_file.parents[2]
    checkout_skill = checkout_root / SKILL_FILENAME
    if (checkout_root / "pyproject.toml").is_file() and (
        checkout_root / "src" / "dokutipp"
    ).is_dir() and checkout_skill.is_file():
        return checkout_skill

    data_directory = sysconfig.get_path("data")
    if data_directory:
        installed_skill = Path(data_directory) / SKILL_FILENAME
        if installed_skill.is_file():
            return installed_skill

    raise OnboardingError(
        "The bundled SKILL.md could not be found. Reinstall DokuTipp and try again."
    )


def hermes_skill_root(
    *,
    environment: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    """Return Hermes Agent's user skill root without changing Hermes config."""
    environment = os.environ if environment is None else environment
    hermes_home = environment.get("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home).expanduser() / "skills"
    if home is None:
        home = Path.home()
    return home / ".hermes" / "skills"


def _normalise_root(path: Path) -> Path:
    """Expand a user-supplied root and persist it as an absolute path."""
    return path.expanduser().resolve()


def _require_interactive(input_stream: TextIO) -> None:
    isatty = getattr(input_stream, "isatty", None)
    if not callable(isatty) or not isatty():
        raise OnboardingError(
            "DokuTipp setup needs an interactive terminal. Run `dokutipp setup` "
            "in a terminal before using this command."
        )


def _prompt(input_stream: TextIO, output_stream: TextIO, prompt: str) -> str:
    output_stream.write(prompt)
    output_stream.flush()
    answer = input_stream.readline()
    if answer == "":
        raise OnboardingError("DokuTipp setup was cancelled because no input was received.")
    return answer.strip()


def _prompt_required(
    input_stream: TextIO,
    output_stream: TextIO,
    prompt: str,
) -> str:
    while True:
        answer = _prompt(input_stream, output_stream, prompt)
        if answer:
            return answer
        output_stream.write("Please enter at least one interest.\n")


def _prompt_confirmation(
    input_stream: TextIO,
    output_stream: TextIO,
    prompt: str,
) -> bool:
    while True:
        answer = _prompt(input_stream, output_stream, f"{prompt} [y/N]: ").lower()
        if answer in ("", "n", "no", "nein"):
            return False
        if answer in ("y", "yes", "j", "ja"):
            return True
        output_stream.write("Please answer y or n.\n")


def _prompt_skill_root(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    environment: Optional[Mapping[str, str]],
    home: Optional[Path],
) -> Path:
    default_root = hermes_skill_root(environment=environment, home=home)
    output_stream.write("Which agent do you use?\n")
    output_stream.write(f"  1) Hermes Agent ({default_root})\n")
    output_stream.write("  2) Enter a skill root manually\n")
    while True:
        choice = _prompt(input_stream, output_stream, "Choose 1 or 2: ")
        if choice == "1":
            return _normalise_root(default_root)
        if choice == "2":
            manual_root = _prompt_required(
                input_stream,
                output_stream,
                "Skill root path: ",
            )
            return _normalise_root(Path(manual_root))
        output_stream.write("Please choose 1 or 2.\n")


def _skill_directory(skill_root: Path) -> Path:
    if skill_root.exists() and not skill_root.is_dir():
        raise OnboardingError(f"Skill root is not a directory: {skill_root}")
    try:
        skill_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise OnboardingError(f"Could not create skill root {skill_root}: {error}") from error

    directory = skill_root / SKILL_DIRECTORY_NAME
    if directory.exists() and not directory.is_dir():
        raise OnboardingError(f"DokuTipp skill path is not a directory: {directory}")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise OnboardingError(
            f"Could not create DokuTipp skill directory {directory}: {error}"
        ) from error
    return directory


def load_config(config_file: Path) -> Optional[AppConfig]:
    """Read and validate DokuTipp's per-user config."""
    if config_file.is_symlink():
        raise OnboardingError(f"DokuTipp config must not be a symbolic link: {config_file}")
    if not config_file.exists():
        return None
    if not config_file.is_file():
        raise OnboardingError(f"DokuTipp config is not a file: {config_file}")
    try:
        value = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OnboardingError(
            f"Could not read DokuTipp config {config_file}: {error}"
        ) from error

    if not isinstance(value, dict) or value.get("agent") != "hermes":
        raise OnboardingError(f"DokuTipp config is invalid: {config_file}")
    skill_root = value.get("skill_root")
    if not isinstance(skill_root, str) or not skill_root:
        raise OnboardingError(f"DokuTipp config is invalid: {config_file}")
    root = Path(skill_root).expanduser()
    if not root.is_absolute():
        raise OnboardingError(f"DokuTipp config has no absolute skill root: {config_file}")
    if "sender_filter_file" not in value:
        sender_filter = None
    else:
        sender_filter_value = value["sender_filter_file"]
        if not isinstance(sender_filter_value, str) or not sender_filter_value:
            raise OnboardingError(f"DokuTipp config is invalid: {config_file}")
        sender_filter = Path(sender_filter_value).expanduser()
        if not sender_filter.is_absolute():
            raise OnboardingError(
                f"DokuTipp config has no absolute sender filter path: {config_file}"
            )
    return AppConfig(skill_root=root, sender_filter_file=sender_filter)


def _write_config(
    config_file: Path,
    skill_root: Path,
    sender_filter_file: Path,
) -> None:
    if config_file.is_symlink():
        raise OnboardingError(f"DokuTipp config must not be a symbolic link: {config_file}")
    try:
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(
                {
                    "agent": "hermes",
                    "skill_root": str(skill_root),
                    "sender_filter_file": str(sender_filter_file),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise OnboardingError(
            f"Could not save DokuTipp config {config_file}: {error}"
        ) from error


def _sender_key(sender: object) -> str:
    return str(sender).strip().casefold()


def _deduplicate_senders(senders: Sequence[str]) -> Tuple[str, ...]:
    values = []
    seen = set()
    for value in senders:
        sender = str(value).strip()
        key = _sender_key(sender)
        if not key or key in seen:
            continue
        seen.add(key)
        values.append(sender)
    return tuple(values)


def _write_sender_filters(filter_file: Path, senders: Sequence[str]) -> None:
    if filter_file.is_symlink():
        raise OnboardingError(
            f"Sender filter file must not be a symbolic link: {filter_file}"
        )
    if filter_file.exists() and not filter_file.is_file():
        raise OnboardingError(
            f"Sender filter path is not a regular file: {filter_file}"
        )
    try:
        filter_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise OnboardingError(
            f"Could not create sender filter directory {filter_file.parent}: {error}"
        ) from error

    temporary_path: Optional[Path] = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{filter_file.name}.",
            suffix=".tmp",
            dir=filter_file.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for sender in _deduplicate_senders(senders):
                handle.write(f"{sender}\n")
        os.replace(temporary_path, filter_file)
        temporary_path = None
    except OSError as error:
        raise OnboardingError(
            f"Could not save sender filter file {filter_file}: {error}"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _validate_sender_filter_path(filter_file: Path) -> None:
    if filter_file.is_symlink():
        raise OnboardingError(
            f"Sender filter file must not be a symbolic link: {filter_file}"
        )
    if filter_file.exists() and not filter_file.is_file():
        raise OnboardingError(
            f"Sender filter path is not a regular file: {filter_file}"
        )


def _ensure_sender_filter_file(filter_file: Path, output_stream: TextIO) -> None:
    _validate_sender_filter_path(filter_file)
    if not filter_file.exists():
        _write_sender_filters(filter_file, ())
        output_stream.write(f"Created empty sender filter file at {filter_file}.\n")
        return
    try:
        load_sender_filters(filter_file)
    except FilterConfigError as error:
        _write_sender_filters(filter_file, ())
        output_stream.write(
            f"Warning: {error} Recreated the sender filter file empty.\n"
        )


def _sender_filters_for_setup(
    filter_file: Path,
    output_stream: TextIO,
) -> Tuple[str, ...]:
    _validate_sender_filter_path(filter_file)
    if not filter_file.exists():
        return ()
    try:
        return load_sender_filters(filter_file)
    except FilterConfigError as error:
        output_stream.write(
            f"Warning: {error} The file will be recreated if setup completes.\n"
        )
        return ()


def _setup_sender_catalog(data_dir: Path, output_stream: TextIO) -> Tuple[str, ...]:
    validated_channels: Tuple[str, ...] = ()

    def validate(filmliste: Path) -> bool:
        nonlocal validated_channels
        try:
            channels = available_channels(filmliste)
        except (
            OSError,
            EOFError,
            UnicodeError,
            ValueError,
            IndexError,
            TypeError,
            lzma.LZMAError,
        ):
            return False
        if not channels:
            return False
        validated_channels = channels
        return True

    def setup_log(message: str) -> None:
        output_stream.write(f"{message}\n")
        output_stream.flush()

    try:
        ensure_filmliste(
            data_dir,
            allow_stale=True,
            validate_existing=True,
            log=setup_log,
            validator=validate,
        )
    except FilmlisteError as error:
        raise OnboardingError(
            f"Could not prepare the broadcaster selection: {error}"
        ) from error
    return validated_channels


def _merge_sender_catalog(
    channels: Sequence[str],
    existing_exclusions: Sequence[str],
) -> Tuple[str, ...]:
    catalog = {}
    for sender in (*channels, *existing_exclusions):
        value = str(sender).strip()
        key = _sender_key(value)
        if key and key not in catalog:
            catalog[key] = value
    return tuple(sorted(catalog.values(), key=_sender_key))


def _questionary_sender_selector(
    channels: Sequence[str],
    default_allowed: Sequence[str],
    *,
    input_stream: TextIO,
    output_stream: TextIO,
) -> Optional[Sequence[str]]:
    try:
        import questionary
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input.defaults import create_input
        from prompt_toolkit.output.defaults import create_output
        from questionary.prompts import common as questionary_common
    except ImportError as error:
        raise OnboardingError(
            "The interactive sender selection is unavailable. Reinstall DokuTipp."
        ) from error

    allowed_keys = {_sender_key(sender) for sender in default_allowed}
    choices = [
        questionary.Choice(sender, checked=_sender_key(sender) in allowed_keys)
        for sender in channels
    ]
    # Questionary 2.x does not expose its checkbox indicators as prompt options.
    # Keep the display override scoped to this dialog and restore it afterwards.
    previous_selected_indicator = questionary_common.INDICATOR_SELECTED
    previous_unselected_indicator = questionary_common.INDICATOR_UNSELECTED
    try:
        questionary_common.INDICATOR_SELECTED = "- [x]"
        questionary_common.INDICATOR_UNSELECTED = "- [ ]"
        try:
            with create_app_session(
                input=create_input(stdin=input_stream),
                output=create_output(stdout=output_stream),
            ):
                return questionary.checkbox(
                    "Which broadcasters should DokuTipp include?",
                    choices=choices,
                    style=questionary.Style([("selected", "noreverse")]),
                    instruction=(
                        "Use arrow keys to move, space to toggle, and enter to "
                        "confirm. - [x] means included; - [ ] means excluded."
                    ),
                ).ask()
        finally:
            questionary_common.INDICATOR_SELECTED = previous_selected_indicator
            questionary_common.INDICATOR_UNSELECTED = previous_unselected_indicator
    except (EOFError, KeyboardInterrupt) as error:
        raise OnboardingError("DokuTipp setup was cancelled.") from error


def _choose_sender_exclusions(
    catalog: Sequence[str],
    existing_exclusions: Sequence[str],
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    sender_selector: Optional[SenderSelector],
) -> Tuple[str, ...]:
    excluded_keys = {_sender_key(sender) for sender in existing_exclusions}
    default_allowed = [
        sender for sender in catalog if _sender_key(sender) not in excluded_keys
    ]
    if sender_selector is None:
        allowed = _questionary_sender_selector(
            catalog,
            default_allowed,
            input_stream=input_stream,
            output_stream=output_stream,
        )
    else:
        allowed = sender_selector(catalog, default_allowed)
    if allowed is None:
        raise OnboardingError("DokuTipp setup was cancelled.")

    catalog_keys = {_sender_key(sender) for sender in catalog}
    allowed_keys = {_sender_key(sender) for sender in allowed}
    if not allowed_keys <= catalog_keys:
        raise OnboardingError("The sender selection returned an unknown broadcaster.")
    return tuple(
        sender for sender in catalog if _sender_key(sender) not in allowed_keys
    )


def _ensure_data_directory(data_dir: Path) -> None:
    if data_dir.exists() and not data_dir.is_dir():
        raise OnboardingError(f"DokuTipp data path is not a directory: {data_dir}")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise OnboardingError(
            f"Could not create DokuTipp data directory {data_dir}: {error}"
        ) from error


def _canonical_skill_bytes(canonical_skill_file: Optional[Path]) -> bytes:
    path = canonical_skill_path() if canonical_skill_file is None else canonical_skill_file
    if not path.is_file():
        raise OnboardingError(f"The bundled SKILL.md is not a file: {path}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise OnboardingError(f"Could not read bundled SKILL.md {path}: {error}") from error


def _write_skill(skill_file: Path, skill_bytes: bytes) -> None:
    try:
        skill_file.write_bytes(skill_bytes)
    except OSError as error:
        raise OnboardingError(f"Could not write SKILL.md {skill_file}: {error}") from error


def _write_profile(profile_file: Path, interests: str, avoid: str) -> None:
    try:
        profile_file.write_text(
            PROFILE_TEMPLATE.format(interests=interests, avoid=avoid),
            encoding="utf-8",
        )
    except OSError as error:
        raise OnboardingError(f"Could not write PROFILE.md {profile_file}: {error}") from error


def _ensure_skill_file(
    skill_file: Path,
    skill_bytes: bytes,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    allow_modified_replacement: bool = False,
) -> None:
    if skill_file.is_symlink():
        raise OnboardingError(f"SKILL.md must not be a symbolic link: {skill_file}")
    if not skill_file.exists():
        _write_skill(skill_file, skill_bytes)
        output_stream.write(f"Installed SKILL.md at {skill_file}.\n")
        return
    if not skill_file.is_file():
        raise OnboardingError(f"SKILL.md is not a regular file: {skill_file}")
    try:
        installed_bytes = skill_file.read_bytes()
    except OSError as error:
        raise OnboardingError(f"Could not read SKILL.md {skill_file}: {error}") from error
    if installed_bytes == skill_bytes:
        return
    if hashlib.sha256(installed_bytes).hexdigest() in KNOWN_CANONICAL_SKILL_SHA256S:
        _write_skill(skill_file, skill_bytes)
        output_stream.write(f"Updated SKILL.md at {skill_file}.\n")
        return

    if not allow_modified_replacement:
        raise OnboardingError(
            f"SKILL.md at {skill_file} contains local changes and was left "
            "unchanged. Run `dokutipp setup` to review or replace it."
        )

    _require_interactive(input_stream)
    if _prompt_confirmation(
        input_stream,
        output_stream,
        f"SKILL.md at {skill_file} differs from the DokuTipp original. Replace it?",
    ):
        _write_skill(skill_file, skill_bytes)
        output_stream.write(f"Restored SKILL.md at {skill_file}.\n")
    else:
        output_stream.write(f"Kept modified SKILL.md at {skill_file}.\n")


def _ensure_profile_file(
    profile_file: Path,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    interests: Optional[str] = None,
    avoid: Optional[str] = None,
    replace_existing: bool = False,
) -> None:
    if profile_file.is_symlink():
        raise OnboardingError(f"PROFILE.md must not be a symbolic link: {profile_file}")
    if profile_file.exists():
        if not profile_file.is_file():
            raise OnboardingError(f"PROFILE.md is not a regular file: {profile_file}")
        if not replace_existing:
            return
        _require_interactive(input_stream)
        if not _prompt_confirmation(
            input_stream,
            output_stream,
            f"PROFILE.md already exists at {profile_file}. Replace it?",
        ):
            output_stream.write(f"Kept existing PROFILE.md at {profile_file}.\n")
            return

    _require_interactive(input_stream)
    if interests is None:
        interests = _prompt_required(
            input_stream,
            output_stream,
            INTERESTS_PROMPT,
        )
    if avoid is None:
        avoid = _prompt(
            input_stream,
            output_stream,
            AVOID_PROMPT,
        )
    _write_profile(profile_file, interests, avoid)
    output_stream.write(f"Saved PROFILE.md at {profile_file}.\n")


def run_setup(
    *,
    config_file: Optional[Path] = None,
    data_dir: Optional[Path] = None,
    input_stream: Optional[TextIO] = None,
    output_stream: Optional[TextIO] = None,
    canonical_skill_file: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    sender_selector: Optional[SenderSelector] = None,
) -> Path:
    """Interactively choose a skill root and install or reconfigure DokuTipp."""
    if input_stream is None:
        import sys

        input_stream = sys.stdin
    if output_stream is None:
        import sys

        output_stream = sys.stderr
    if config_file is None:
        config_file = config_path(environment=environment, home=home)
    if data_dir is None:
        data_dir = data_directory(home)

    _require_interactive(input_stream)
    output_stream.write("Welcome to DokuTipp. Let's set up your profile.\n")
    existing_config = load_config(config_file)
    sender_filter = (
        existing_config.sender_filter_file
        if existing_config is not None
        and existing_config.sender_filter_file is not None
        else _normalise_root(default_sender_filter_file(home))
    )
    existing_exclusions = _sender_filters_for_setup(sender_filter, output_stream)
    current_channels = _setup_sender_catalog(data_dir, output_stream)
    sender_catalog = _merge_sender_catalog(current_channels, existing_exclusions)
    interests = _prompt_required(
        input_stream,
        output_stream,
        INTERESTS_PROMPT,
    )
    skill_root = _prompt_skill_root(
        input_stream,
        output_stream,
        environment=environment,
        home=home,
    )
    avoid = _prompt(
        input_stream,
        output_stream,
        AVOID_PROMPT,
    )
    excluded_channels = _choose_sender_exclusions(
        sender_catalog,
        existing_exclusions,
        input_stream=input_stream,
        output_stream=output_stream,
        sender_selector=sender_selector,
    )

    skill_directory = _skill_directory(skill_root)
    skill_bytes = _canonical_skill_bytes(canonical_skill_file)
    _ensure_skill_file(
        skill_directory / SKILL_FILENAME,
        skill_bytes,
        input_stream=input_stream,
        output_stream=output_stream,
        allow_modified_replacement=True,
    )
    _ensure_profile_file(
        skill_directory / PROFILE_FILENAME,
        input_stream=input_stream,
        output_stream=output_stream,
        interests=interests,
        avoid=avoid,
        replace_existing=True,
    )
    _ensure_data_directory(data_dir)
    _write_sender_filters(sender_filter, excluded_channels)
    _write_config(config_file, skill_root, sender_filter)
    output_stream.write("DokuTipp setup is complete.\n")
    return skill_root


def ensure_installation(
    *,
    config_file: Optional[Path] = None,
    data_dir: Optional[Path] = None,
    input_stream: Optional[TextIO] = None,
    output_stream: Optional[TextIO] = None,
    canonical_skill_file: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    sender_selector: Optional[SenderSelector] = None,
) -> bool:
    """Ensure the configured skill is available; return whether setup just ran."""
    if input_stream is None:
        import sys

        input_stream = sys.stdin
    if output_stream is None:
        import sys

        output_stream = sys.stderr
    if config_file is None:
        config_file = config_path(environment=environment, home=home)
    if data_dir is None:
        data_dir = data_directory(home)

    app_config = load_config(config_file)
    if app_config is None:
        run_setup(
            config_file=config_file,
            data_dir=data_dir,
            input_stream=input_stream,
            output_stream=output_stream,
            canonical_skill_file=canonical_skill_file,
            environment=environment,
            home=home,
            sender_selector=sender_selector,
        )
        return True

    skill_root = app_config.skill_root
    _ensure_data_directory(data_dir)
    if app_config.sender_filter_file is not None:
        _ensure_sender_filter_file(app_config.sender_filter_file, output_stream)
    skill_directory = _skill_directory(skill_root)
    skill_bytes = _canonical_skill_bytes(canonical_skill_file)
    _ensure_skill_file(
        skill_directory / SKILL_FILENAME,
        skill_bytes,
        input_stream=input_stream,
        output_stream=output_stream,
    )
    _ensure_profile_file(
        skill_directory / PROFILE_FILENAME,
        input_stream=input_stream,
        output_stream=output_stream,
    )
    return False
