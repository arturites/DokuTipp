"""Stable candidate IDs and selection validation for DokuTipp."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence, Tuple


NORMAL_RECOMMENDATION_COUNT = 3
TOTAL_RECOMMENDATION_COUNT = NORMAL_RECOMMENDATION_COUNT + 1
EXTRA_ID_PREFIX = "x"
CANDIDATE_HASH_FIELDS = ("title", "duration", "channel", "date", "website")
CANDIDATE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class SelectionError(ValueError):
    """Raised when candidate IDs or a submitted selection are unusable."""


@dataclass(frozen=True)
class ResolvedSelection:
    """The original source records selected by the agent."""

    recommendations: Tuple[Mapping[str, Any], ...]
    extra_recommendation: Mapping[str, Any]


def candidate_id(candidate: Mapping[str, Any]) -> str:
    """Return the stable full SHA-256 ID for one source candidate."""
    identity = {
        field: _text(candidate.get(field))
        for field in CANDIDATE_HASH_FIELDS
    }
    canonical_json = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def build_candidate_registry(
    candidates: Sequence[Mapping[str, Any]],
) -> Dict[str, Mapping[str, Any]]:
    """Map stable IDs to source records and reject ambiguous IDs."""
    registry: Dict[str, Mapping[str, Any]] = {}
    for candidate in candidates:
        identifier = candidate_id(candidate)
        if identifier in registry:
            raise SelectionError(
                f"Ambiguous candidate ID {identifier!r} maps to multiple candidates."
            )
        registry[identifier] = candidate
    return registry


def build_fetch_payload(
    candidates: Sequence[Mapping[str, Any]],
    *,
    limit: int,
    min_duration: int,
    channels: Sequence[str],
    title_filters: Sequence[str] = (),
    message: str = "",
) -> Dict[str, Any]:
    """Build the machine-readable candidate payload emitted by fetch."""
    registry = build_candidate_registry(candidates)
    candidate_payload = []
    for identifier, candidate in registry.items():
        candidate_payload.append(
            {
                "id": identifier,
                "title": _text(candidate.get("title")),
                "channel": _text(candidate.get("channel")),
                "date": _text(candidate.get("date")),
                "duration": _text(candidate.get("duration")),
                "description": _text(candidate.get("description")),
            }
        )

    if not candidates:
        status = "no_candidates"
    elif len(candidates) < TOTAL_RECOMMENDATION_COUNT:
        status = "insufficient_candidates"
    else:
        status = "ready"

    payload: Dict[str, Any] = {
        "status": status,
        "selection": {
            "normal_recommendations": NORMAL_RECOMMENDATION_COUNT,
            "extra_recommendations": 1,
            "extra_id_prefix": EXTRA_ID_PREFIX,
            "argument_format": "ID1,ID2,ID3,xID4",
        },
        "filters": {
            "limit": limit,
            "min_duration": min_duration,
            "channels": list(channels),
            "title_exclusions": list(title_filters),
        },
        "candidates": candidate_payload,
    }
    if status != "ready":
        payload["message"] = message
    return payload


def resolve_selection(
    selection_argument: str,
    candidates: Sequence[Mapping[str, Any]],
) -> ResolvedSelection:
    """Validate a comma-separated 3+1 selection and resolve source records."""
    recommendation_ids, extra_recommendation_id = parse_selection_argument(
        selection_argument
    )
    registry = build_candidate_registry(candidates)

    selected_ids = [*recommendation_ids, extra_recommendation_id]
    unknown_ids = [
        identifier for identifier in selected_ids if identifier not in registry
    ]
    if unknown_ids:
        raise SelectionError(f"Unknown candidate ID {unknown_ids[0]!r}.")

    return ResolvedSelection(
        recommendations=tuple(registry[identifier] for identifier in recommendation_ids),
        extra_recommendation=registry[extra_recommendation_id],
    )


def parse_selection_argument(selection_argument: str) -> Tuple[Tuple[str, ...], str]:
    """Parse exactly three normal IDs and one arbitrarily positioned x ID."""
    parts = [part.strip() for part in selection_argument.split(",")]
    if len(parts) != TOTAL_RECOMMENDATION_COUNT:
        raise SelectionError(
            "Selection must contain exactly four comma-separated candidate IDs."
        )
    if any(not part for part in parts):
        raise SelectionError("Selection contains an empty candidate ID.")

    extra_parts = [part for part in parts if part.startswith(EXTRA_ID_PREFIX)]
    if len(extra_parts) != 1:
        raise SelectionError(
            "Selection must contain exactly one extra candidate prefixed with 'x'."
        )

    normal_ids = []
    extra_id = ""
    all_ids = []
    for part in parts:
        is_extra = part.startswith(EXTRA_ID_PREFIX)
        identifier = part[1:] if is_extra else part
        if not CANDIDATE_ID_PATTERN.fullmatch(identifier):
            raise SelectionError(
                "Candidate IDs must be complete lowercase SHA-256 hashes; "
                "the extra candidate must use a lowercase 'x' prefix."
            )
        all_ids.append(identifier)
        if is_extra:
            extra_id = identifier
        else:
            normal_ids.append(identifier)

    if len(set(all_ids)) != len(all_ids):
        raise SelectionError("Selection contains a duplicate candidate ID.")
    if len(normal_ids) != NORMAL_RECOMMENDATION_COUNT:
        raise SelectionError(
            f"Selection must contain exactly {NORMAL_RECOMMENDATION_COUNT} "
            "normal candidate IDs."
        )

    return tuple(normal_ids), extra_id


def _text(value: Any) -> str:
    """Return the canonical text representation used by IDs and fetch output."""
    if value is None:
        return ""
    return str(value)
