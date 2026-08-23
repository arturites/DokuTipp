"""Stable candidate IDs and selection validation for DokuTipp."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import AbstractSet, Any, Dict, Mapping, Optional, Sequence, Tuple


NORMAL_RECOMMENDATION_COUNT = 3
TOTAL_RECOMMENDATION_COUNT = NORMAL_RECOMMENDATION_COUNT + 1
EXTRA_ID_PREFIX = "x"
CANDIDATE_HASH_FIELDS = ("title", "duration", "channel", "date", "website")
CANDIDATE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class SelectionError(ValueError):
    """Raised when candidate IDs or a submitted selection are unusable."""


class PaginationError(ValueError):
    """Raised when a requested candidate page is outside the available range."""

    def __init__(
        self,
        message: str,
        *,
        page: int,
        total_pages: int,
        limit: int,
        total_candidates: int,
    ) -> None:
        super().__init__(message)
        self.page = page
        self.total_pages = total_pages
        self.limit = limit
        self.total_candidates = total_candidates


@dataclass(frozen=True)
class ResolvedSelection:
    """The original source records selected by the agent."""

    recommendations: Tuple[Mapping[str, Any], ...]
    extra_recommendation: Mapping[str, Any]


@dataclass(frozen=True)
class CandidatePage:
    """One deterministic, one-based page of the candidate pool."""

    candidates: Tuple[Mapping[str, Any], ...]
    page: int
    total_pages: int
    limit: int
    total_candidates: int
    start: int
    end: int

    def pagination_payload(self) -> Dict[str, Any]:
        """Return machine-readable metadata for one page."""
        return {
            "page": self.page,
            "total_pages": self.total_pages,
            "limit": self.limit,
            "total_candidates": self.total_candidates,
            "candidate_range": {"start": self.start, "end": self.end},
        }


def candidate_id(candidate: Mapping[str, Any]) -> str:
    """Return the stable full SHA-256 ID for one source candidate."""
    identity = _candidate_identity(candidate)
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
    """Map logical candidates once and reject true hash collisions."""
    registry: Dict[str, Mapping[str, Any]] = {}
    for candidate in candidates:
        identifier = candidate_id(candidate)
        if identifier in registry:
            if _candidate_identity(registry[identifier]) != _candidate_identity(
                candidate
            ):
                raise SelectionError(
                    f"Ambiguous candidate ID {identifier!r} maps to multiple candidates."
                )
            continue
        registry[identifier] = candidate
    return registry


def paginate_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    limit: int,
    page: int,
) -> CandidatePage:
    """Return a stable page after validating the requested bounds."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise SelectionError("limit must be a positive integer.")
    if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
        raise SelectionError("page must be a positive integer.")

    ordered_candidates = tuple(sorted(candidates, key=candidate_id))
    total_candidates = len(ordered_candidates)
    total_pages = (
        (total_candidates + limit - 1) // limit if total_candidates else 0
    )
    if total_candidates == 0 and page == 1:
        return CandidatePage(
            candidates=(),
            page=page,
            total_pages=total_pages,
            limit=limit,
            total_candidates=total_candidates,
            start=0,
            end=0,
        )
    if page > total_pages:
        raise PaginationError(
            f"Page {page} is outside the available range 1..{total_pages}.",
            page=page,
            total_pages=total_pages,
            limit=limit,
            total_candidates=total_candidates,
        )

    offset = (page - 1) * limit
    visible_candidates = ordered_candidates[offset : offset + limit]
    return CandidatePage(
        candidates=visible_candidates,
        page=page,
        total_pages=total_pages,
        limit=limit,
        total_candidates=total_candidates,
        start=offset + 1,
        end=offset + len(visible_candidates),
    )


def build_candidate_pool(
    candidates: Sequence[Mapping[str, Any]],
    *,
    excluded_ids: AbstractSet[str] = frozenset(),
) -> list:
    """Return unique candidates excluding the supplied recommendation IDs."""
    registry = build_candidate_registry(candidates)
    return [
        candidate
        for identifier, candidate in registry.items()
        if identifier not in excluded_ids
    ]


def build_fetch_payload(
    candidates: Sequence[Mapping[str, Any]],
    *,
    limit: int,
    page: int = 1,
    min_duration: int,
    excluded_channels: Sequence[str],
    title_filters: Sequence[str] = (),
    excluded_ids: AbstractSet[str] = frozenset(),
    message: str = "",
) -> Dict[str, Any]:
    """Build the machine-readable candidate payload emitted by fetch.

    Candidate IDs are excluded only after the registry has checked the complete
    source set for duplicate rows and genuine hash collisions.
    """
    registry = build_candidate_registry(candidates)
    candidate_pool = [
        candidate
        for identifier, candidate in registry.items()
        if identifier not in excluded_ids
    ]
    candidate_page = paginate_candidates(candidate_pool, limit=limit, page=page)
    candidate_payload = []
    for candidate in candidate_page.candidates:
        candidate_payload.append(
            {
                "id": candidate_id(candidate),
                "title": _text(candidate.get("title")),
                "channel": _text(candidate.get("channel")),
                "date": _text(candidate.get("date")),
                "duration": _text(candidate.get("duration")),
                "description": _text(candidate.get("description")),
            }
        )

    available = len(candidate_pool)
    if not available:
        status = "no_candidates"
    elif available < TOTAL_RECOMMENDATION_COUNT:
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
            "page": page,
            "min_duration": min_duration,
            "excluded_channels": list(excluded_channels),
            "title_exclusions": list(title_filters),
        },
        "pagination": candidate_page.pagination_payload(),
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


def _candidate_identity(candidate: Mapping[str, Any]) -> Dict[str, str]:
    """Return the normalized fields that define one logical candidate."""
    return {
        field: _text(candidate.get(field))
        for field in CANDIDATE_HASH_FIELDS
    }
