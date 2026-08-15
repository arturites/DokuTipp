"""Install and maintain DokuTipp's local agent skill."""

from __future__ import annotations

import json
import os
import sysconfig
from pathlib import Path
from typing import Mapping, Optional, TextIO


CONFIG_RELATIVE_PATH = Path("dokutipp") / "config.json"
PROFILE_FILENAME = "PROFILE.md"
SKILL_DIRECTORY_NAME = "dokutipp"
SKILL_FILENAME = "SKILL.md"
INTERESTS_PROMPT = (
    "What documentary topics interest you? "
    "Enter them comma-separated on one line (for example: history, science, nature): "
)
AVOID_PROMPT = (
    "Which topics would you like to avoid? "
    "Enter them comma-separated on one line (optional): "
)


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
    environment = os.environ if environment is None else environment
    configured_root = environment.get("XDG_CONFIG_HOME")
    if configured_root:
        return Path(configured_root).expanduser() / CONFIG_RELATIVE_PATH
    if home is None:
        home = Path.home()
    return home / ".config" / CONFIG_RELATIVE_PATH


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


def _load_config(config_file: Path) -> Optional[Path]:
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
    return root


def _write_config(config_file: Path, skill_root: Path) -> None:
    if config_file.is_symlink():
        raise OnboardingError(f"DokuTipp config must not be a symbolic link: {config_file}")
    try:
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(
                {"agent": "hermes", "skill_root": str(skill_root)},
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
    input_stream: Optional[TextIO] = None,
    output_stream: Optional[TextIO] = None,
    canonical_skill_file: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
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

    _require_interactive(input_stream)
    output_stream.write("Welcome to DokuTipp. Let's set up your profile.\n")
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
        interests=interests,
        avoid=avoid,
        replace_existing=True,
    )
    _write_config(config_file, skill_root)
    output_stream.write("DokuTipp setup is complete.\n")
    return skill_root


def ensure_installation(
    *,
    config_file: Optional[Path] = None,
    input_stream: Optional[TextIO] = None,
    output_stream: Optional[TextIO] = None,
    canonical_skill_file: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
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

    skill_root = _load_config(config_file)
    if skill_root is None:
        run_setup(
            config_file=config_file,
            input_stream=input_stream,
            output_stream=output_stream,
            canonical_skill_file=canonical_skill_file,
            environment=environment,
            home=home,
        )
        return True

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
