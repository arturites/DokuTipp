"""Deterministic user-facing rendering for DokuTipp recommendations."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Optional

from .selection import ResolvedSelection


def render_recommendations(
    selection: ResolvedSelection, *, today: date
) -> str:
    """Render the complete DokuTipp Markdown result from source records."""
    sections = [
        f"# 📺 DokuTipps der Woche – {today.isoformat()}",
        "",
        "## Empfehlungen",
    ]

    recommendation_count = len(selection.recommendations)
    for number, candidate in enumerate(selection.recommendations, start=1):
        sections.extend(["", _render_recommendation(candidate, number=number)])
        if number < recommendation_count:
            sections.extend(["", "---"])

    sections.extend(
        [
            "",
            "---",
            "",
            "## 🔭 Extra-Empfehlung",
            "",
            _render_recommendation(selection.extra_recommendation),
        ]
    )
    return "\n".join(sections) + "\n"


def render_no_candidates(*, today: date) -> str:
    """Render the final user-facing result when filtering finds no candidates."""
    return (
        f"# 📺 DokuTipps der Woche – {today.isoformat()}\n\n"
        "Keine passenden Dokumentationen wurden in der aktuellen MediathekView-Liste gefunden.\n"
    )


def render_insufficient_candidates(*, available: int, required: int, today: date) -> str:
    """Render a complete result when no valid 3+1 selection is possible."""
    return (
        f"# 📺 DokuTipps der Woche – {today.isoformat()}\n\n"
        "Für eine vollständige Ausgabe fehlen passende Dokumentationen in der "
        f"aktuellen MediathekView-Liste ({available} von {required} benötigt).\n"
    )


def format_duration(value: Any) -> str:
    """Format MediathekView HH:MM:SS durations for a German result."""
    duration = _text(value)
    if not duration:
        return "unbekannt"

    parts = duration.split(":")
    if len(parts) != 3:
        return duration
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return duration
    if hours < 0 or minutes < 0 or seconds < 0:
        return duration

    components = []
    if hours:
        components.append(f"{hours} Std.")
    if minutes or hours:
        components.append(f"{minutes} Min.")
    elif seconds:
        components.append(f"{seconds} Sek.")
    return " ".join(components) or "0 Min."


def _render_recommendation(
    candidate: Mapping[str, Any], *, number: Optional[int] = None
) -> str:
    title = _text(candidate.get("title")) or "Ohne Titel"
    heading = f"### {number}. 🎬 {title}" if number is not None else f"### 🎬 {title}"
    channel = _text(candidate.get("channel")) or "unbekannt"
    broadcast_date = _text(candidate.get("date")) or "unbekannt"
    description = _text(candidate.get("description")) or "Keine Beschreibung verfügbar."

    lines = [
        heading,
        f"📡 Sender: {channel}",
        f"⏱ Laufzeit: {format_duration(candidate.get('duration'))}",
        f"📅 Datum: {broadcast_date}",
        "",
        description,
    ]
    website = _text(candidate.get("website"))
    if website:
        lines.extend(["", f"🔗 [Zur Mediathek]({website})"])
    return "\n".join(lines)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
