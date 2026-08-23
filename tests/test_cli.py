import io
import importlib.util
import json
import lzma
import os
import pty
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from dokutipp import cli, filmliste, history, onboarding, parser, selection
from dokutipp.parser import FilterConfigError
from dokutipp.rendering import format_duration, render_recommendations
from dokutipp.selection import (
    CANDIDATE_HASH_FIELDS,
    EXTRA_ID_PREFIX,
    SelectionError,
    build_candidate_registry,
    candidate_id,
    parse_selection_argument,
    resolve_selection,
)


class InteractiveInput(io.StringIO):
    """An in-memory terminal stream for onboarding tests."""

    def isatty(self):
        return True


class CandidateIdTests(unittest.TestCase):
    def test_candidate_id_is_stable_for_non_identity_changes_and_mapping_order(self):
        candidate = make_candidate(
            "Stable documentary",
            channel="ARTE.DE",
            date="10.08.2026",
            duration="01:02:03",
            description="Original synopsis.",
            website="https://example.invalid/stable",
        )
        reordered_candidate = {
            "website": candidate["website"],
            "description": candidate["description"],
            "duration": candidate["duration"],
            "date": candidate["date"],
            "channel": candidate["channel"],
            "title": candidate["title"],
        }
        changed_description = dict(candidate, description="Updated synopsis.")
        with_unrelated_data = dict(candidate, internal_note="not part of identity")

        identifier = candidate_id(candidate)

        self.assertEqual(
            identifier,
            "5027a85a3d96931a74c6af12e60c7259413f1d433b80e3a4ae72047e565d689e",
        )
        self.assertEqual(len(identifier), 64)
        self.assertRegex(identifier, r"^[0-9a-f]{64}$")
        self.assertEqual(identifier, candidate_id(reordered_candidate))
        self.assertEqual(identifier, candidate_id(changed_description))
        self.assertEqual(identifier, candidate_id(with_unrelated_data))

    def test_candidate_id_changes_when_any_identity_field_changes(self):
        candidate = make_candidate("Identity documentary")
        original_identifier = candidate_id(candidate)

        for field in CANDIDATE_HASH_FIELDS:
            with self.subTest(field=field):
                changed = dict(candidate)
                changed[field] = f"{changed[field]} changed"
                self.assertNotEqual(original_identifier, candidate_id(changed))

    def test_missing_and_none_identity_values_are_both_empty_strings(self):
        missing_values = {}
        none_values = {field: None for field in CANDIDATE_HASH_FIELDS}

        self.assertEqual(candidate_id(missing_values), candidate_id(none_values))

    def test_registry_collapses_repeated_rows_with_the_reported_candidate_id(self):
        first = make_candidate(
            "Charlotte Link – Der Beobachter",
            channel="ARD",
            date="13.08.2026",
            duration="01:34:08",
            description="First source synopsis.",
            website=(
                "https://www.ardmediathek.de/video/"
                "Y3JpZDovL2FyZC5kZS9wbGFuQVJEXzhhNmIzNTY1LTJjM2QtNDgwZS1iODBk"
                "LTliNThmZjczNWFjZV9nYW56ZVNlbmR1bmc"
            ),
        )
        repeated = dict(first, description="Updated source synopsis.")
        identifier = candidate_id(first)

        self.assertEqual(
            identifier,
            "80fda51bfcad1147552f49cd083d44e63d70ad2847ab3015c080b5c9cc7c3749",
        )
        self.assertEqual(identifier, candidate_id(repeated))

        registry = build_candidate_registry([first, repeated])

        self.assertEqual(list(registry), [identifier])
        self.assertIs(registry[identifier], first)

    def test_registry_rejects_a_true_hash_collision(self):
        first = make_candidate("First identity")
        second = make_candidate("Different identity")

        with patch.object(selection, "candidate_id", return_value="a" * 64):
            with self.assertRaisesRegex(SelectionError, "Ambiguous candidate ID"):
                build_candidate_registry([first, second])

            with self.assertRaisesRegex(SelectionError, "Ambiguous candidate ID"):
                selection.build_fetch_payload(
                    [first, second],
                    limit=50,
                    page=1,
                    min_duration=42,
                    excluded_channels=(),
                    excluded_ids={"a" * 64},
                )


class RecommendationHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.history_file = Path(temporary_directory.name) / "recommendation-history.json"
        self.selected_at = 1_800_000_000.0
        self.identifiers = [character * 64 for character in "abc"]

    def test_missing_history_is_empty_without_creating_a_file(self):
        self.assertEqual(
            set(history.load_recent_ids(self.history_file, now=self.selected_at)),
            set(),
        )
        self.assertFalse(self.history_file.exists())

    def test_records_merges_refreshes_and_prunes_ids_at_the_exact_ttl(self):
        first, refreshed, added = self.identifiers
        history.record_selected_ids(
            self.history_file,
            [first, refreshed],
            now=self.selected_at,
        )
        history.record_selected_ids(
            self.history_file,
            [refreshed, added],
            now=self.selected_at + 60,
        )

        self.assertEqual(
            set(
                history.load_recent_ids(
                    self.history_file,
                    now=self.selected_at + history.HISTORY_TTL_SECONDS - 1,
                )
            ),
            {first, refreshed, added},
        )

        self.assertEqual(
            set(
                history.load_recent_ids(
                    self.history_file,
                    now=self.selected_at + history.HISTORY_TTL_SECONDS,
                )
            ),
            {refreshed, added},
        )
        persisted_history = (
            self.history_file.read_text(encoding="utf-8")
            if self.history_file.exists()
            else ""
        )
        self.assertNotIn(first, persisted_history)

        self.assertEqual(
            set(
                history.load_recent_ids(
                    self.history_file,
                    now=self.selected_at + 60 + history.HISTORY_TTL_SECONDS,
                )
            ),
            set(),
        )

    def test_corrupted_history_is_reset_and_can_be_used_again(self):
        self.history_file.write_text("{not valid json", encoding="utf-8")
        warnings = []

        self.assertEqual(
            set(
                history.load_recent_ids(
                    self.history_file,
                    now=self.selected_at,
                    warn=warnings.append,
                )
            ),
            set(),
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("invalid and has been reset", warnings[0])
        self.assertEqual(
            set(
                history.load_recent_ids(
                    self.history_file,
                    now=self.selected_at,
                    warn=warnings.append,
                )
            ),
            set(),
        )
        self.assertEqual(len(warnings), 1)

        identifier = self.identifiers[0]
        history.record_selected_ids(
            self.history_file,
            [identifier],
            now=self.selected_at,
        )
        self.assertEqual(
            set(history.load_recent_ids(self.history_file, now=self.selected_at)),
            {identifier},
        )

    def test_invalid_version_and_future_timestamp_are_not_kept_active(self):
        identifier = self.identifiers[0]
        self.history_file.write_text(
            json.dumps({"version": True, "selected_at": {}}),
            encoding="utf-8",
        )
        warnings = []

        self.assertEqual(
            history.load_recent_ids(
                self.history_file,
                now=self.selected_at,
                warn=warnings.append,
            ),
            set(),
        )
        self.assertEqual(len(warnings), 1)

        self.history_file.write_text(
            json.dumps(
                {
                    "version": history.HISTORY_VERSION,
                    "selected_at": {
                        identifier: self.selected_at + history.HISTORY_TTL_SECONDS * 10
                    },
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            history.load_recent_ids(self.history_file, now=self.selected_at),
            set(),
        )
        self.assertNotIn(identifier, self.history_file.read_text(encoding="utf-8"))

    def test_dangling_history_symlink_is_reset_instead_of_being_ignored(self):
        self.history_file.symlink_to(self.history_file.parent / "missing-history.json")
        warnings = []

        self.assertEqual(
            history.load_recent_ids(
                self.history_file,
                now=self.selected_at,
                warn=warnings.append,
            ),
            set(),
        )
        self.assertFalse(self.history_file.is_symlink())
        self.assertEqual(len(warnings), 1)

    def test_failed_atomic_replace_preserves_the_previous_history(self):
        first, second, _ = self.identifiers
        history.record_selected_ids(
            self.history_file,
            [first],
            now=self.selected_at,
        )
        previous_contents = self.history_file.read_bytes()

        with patch.object(history.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(
                history.RecommendationHistoryError,
                "Could not write recommendation history",
            ):
                history.record_selected_ids(
                    self.history_file,
                    [second],
                    now=self.selected_at + 60,
                )

        self.assertEqual(self.history_file.read_bytes(), previous_contents)


class FetchPayloadTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.history_file = Path(temporary_directory.name) / "recommendation-history.json"
        self.candidates = [
            make_candidate(
                "One",
                channel="ARD",
                description="First source synopsis.",
                website="https://example.invalid/one",
            ),
            make_candidate(
                "Two",
                channel="ZDF",
                description="Second source synopsis.",
                website="https://example.invalid/two",
            ),
            make_candidate(
                "Three",
                channel="ARTE.DE",
                description="Third source synopsis.",
                website="https://example.invalid/three",
            ),
            make_candidate(
                "Extra",
                channel="ARD",
                description="Extra source synopsis.",
                website="https://example.invalid/extra",
            ),
        ]

    def test_fetch_ready_outputs_machine_readable_ids_without_urls(self):
        output = io.StringIO()
        data_dir = Path("/tmp/dokutipp-fetch-data")
        with patch.object(cli, "load_candidates", return_value=self.candidates) as load:
            payload = cli.run_fetch(
                data_dir=data_dir,
                limit=17,
                min_duration=55,
                excluded_channels=("WDR",),
                filter_file=None,
                history_file=self.history_file,
                output=output,
                today=date(2026, 8, 12),
            )

        load.assert_called_once_with(
            data_dir=data_dir,
            min_duration=55,
            excluded_channels=("WDR",),
            filter_file=None,
        )
        self.assertEqual(payload["status"], "ready")
        self.assertNotIn("message", payload)
        self.assertEqual(
            set(payload),
            {"status", "pagination", "candidates"},
        )
        self.assertEqual(
            [candidate["id"] for candidate in payload["candidates"]],
            sorted(candidate_id(candidate) for candidate in self.candidates),
        )
        self.assertEqual(
            payload["pagination"],
            {
                "page": 1,
                "total_pages": 1,
                "limit": 17,
                "total_candidates": 4,
                "candidate_range": {"start": 1, "end": 4},
            },
        )
        self.assertEqual(json.loads(output.getvalue()), payload)
        self.assertNotIn("website", payload["candidates"][0])
        self.assertNotIn(self.candidates[0]["website"], output.getvalue())

    def test_cli_limit_defaults_to_fifty_and_page_defaults_to_one(self):
        arguments = cli.build_argument_parser().parse_args(["fetch"])

        self.assertEqual(arguments.limit, 50)
        self.assertEqual(arguments.page, 1)

        explicit = cli.build_argument_parser().parse_args(
            ["fetch", "--limit", "12", "--page", "3"]
        )
        self.assertEqual(explicit.limit, 12)
        self.assertEqual(explicit.page, 3)

        for option in ("--limit", "--page"):
            for value in ("0", "-1", "not-a-number"):
                with self.subTest(option=option, value=value):
                    with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                        cli.build_argument_parser().parse_args(
                            ["fetch", option, value]
                        )

        output = io.StringIO()
        with patch.object(cli, "load_candidates", return_value=self.candidates) as load:
            cli.run_fetch(
                history_file=self.history_file,
                output=output,
                today=date(2026, 8, 12),
            )

        self.assertNotIn("limit", load.call_args.kwargs)
        self.assertEqual(json.loads(output.getvalue())["pagination"]["limit"], 50)
        self.assertEqual(json.loads(output.getvalue())["pagination"]["page"], 1)

    def test_fetch_reports_no_and_insufficient_candidates_without_urls(self):
        cases = [
            ([], "no_candidates", "Keine passenden Dokumentationen"),
            (
                self.candidates[:3],
                "insufficient_candidates",
                "3 von 4 benötigt",
            ),
        ]

        for candidates, expected_status, expected_message in cases:
            with self.subTest(status=expected_status):
                output = io.StringIO()
                with patch.object(cli, "load_candidates", return_value=candidates):
                    payload = cli.run_fetch(
                        history_file=self.history_file,
                        output=output,
                        today=date(2026, 8, 12),
                    )

                self.assertEqual(payload["status"], expected_status)
                self.assertIn(expected_message, payload["message"])
                self.assertEqual(json.loads(output.getvalue()), payload)
                self.assertNotIn("website", output.getvalue())
                for candidate in candidates:
                    self.assertNotIn(candidate["website"], output.getvalue())

    def test_fetch_counts_repeated_source_rows_as_one_candidate(self):
        candidates = [
            self.candidates[0],
            dict(self.candidates[0], description="Updated source synopsis."),
            self.candidates[1],
            self.candidates[2],
        ]
        output = io.StringIO()

        with patch.object(cli, "load_candidates", return_value=candidates):
            payload = cli.run_fetch(
                history_file=self.history_file,
                output=output,
                today=date(2026, 8, 12),
            )

        self.assertEqual(payload["status"], "insufficient_candidates")
        self.assertEqual(
            [candidate["id"] for candidate in payload["candidates"]],
            sorted(candidate_id(candidate) for candidate in self.candidates[:3]),
        )
        self.assertIn("3 von 4 benötigt", payload["message"])

    def test_fetch_excludes_recent_ids_and_recomputes_status_until_exact_expiry(self):
        selected_at = 1_800_000_000.0
        candidates = [
            *self.candidates,
            make_candidate("Five", website="https://example.invalid/five"),
        ]
        identifiers = [candidate_id(candidate) for candidate in candidates]
        history.record_selected_ids(
            self.history_file,
            identifiers[:2],
            now=selected_at,
        )

        before_expiry = io.StringIO()
        with patch.object(cli, "load_candidates", return_value=candidates):
            active_payload = cli.run_fetch(
                history_file=self.history_file,
                history_now=selected_at + history.HISTORY_TTL_SECONDS - 1,
                output=before_expiry,
                today=date(2026, 8, 12),
            )

        self.assertEqual(active_payload["status"], "insufficient_candidates")
        self.assertEqual(
            [candidate["id"] for candidate in active_payload["candidates"]],
            sorted(identifiers[2:]),
        )
        self.assertIn("3 von 4 benötigt", active_payload["message"])
        self.assertEqual(json.loads(before_expiry.getvalue()), active_payload)
        self.assertNotIn(identifiers[0], before_expiry.getvalue())
        self.assertNotIn(identifiers[1], before_expiry.getvalue())

        at_expiry = io.StringIO()
        with patch.object(cli, "load_candidates", return_value=candidates):
            expired_payload = cli.run_fetch(
                history_file=self.history_file,
                history_now=selected_at + history.HISTORY_TTL_SECONDS,
                output=at_expiry,
                today=date(2026, 8, 12),
            )

        self.assertEqual(expired_payload["status"], "ready")
        self.assertEqual(
            [candidate["id"] for candidate in expired_payload["candidates"]],
            sorted(identifiers),
        )

    def test_fetch_reports_no_candidates_when_every_candidate_is_recent(self):
        selected_at = 1_800_000_000.0
        identifiers = [candidate_id(candidate) for candidate in self.candidates]
        history.record_selected_ids(
            self.history_file,
            identifiers,
            now=selected_at,
        )
        output = io.StringIO()

        with patch.object(cli, "load_candidates", return_value=self.candidates):
            payload = cli.run_fetch(
                history_file=self.history_file,
                history_now=selected_at,
                output=output,
                today=date(2026, 8, 12),
            )

        self.assertEqual(payload["status"], "no_candidates")
        self.assertEqual(payload["candidates"], [])
        self.assertIn("Keine passenden Dokumentationen", payload["message"])

    def test_recent_ids_are_excluded_before_page_slicing(self):
        selected_at = 1_800_000_000.0
        candidates = [
            *self.candidates,
            make_candidate("Five", website="https://example.invalid/five"),
        ]
        identifiers = [candidate_id(candidate) for candidate in candidates]
        history.record_selected_ids(
            self.history_file,
            [identifiers[0]],
            now=selected_at,
        )
        output = io.StringIO()

        with patch.object(cli, "load_candidates", return_value=candidates) as load:
            payload = cli.run_fetch(
                limit=4,
                history_file=self.history_file,
                history_now=selected_at,
                output=output,
                today=date(2026, 8, 12),
            )

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(
            [candidate["id"] for candidate in payload["candidates"]],
            sorted(identifiers[1:]),
        )
        self.assertNotIn(identifiers[0], output.getvalue())
        self.assertNotIn("limit", load.call_args.kwargs)

    def test_corrupted_history_warns_without_contaminating_fetch_json(self):
        self.history_file.write_text("{not valid json", encoding="utf-8")
        output = io.StringIO()
        stderr = io.StringIO()

        with patch.object(cli, "load_candidates", return_value=self.candidates), redirect_stderr(
            stderr
        ):
            payload = cli.run_fetch(
                history_file=self.history_file,
                history_now=1_800_000_000.0,
                output=output,
                today=date(2026, 8, 12),
            )

        self.assertEqual(json.loads(output.getvalue()), payload)
        self.assertIn("Warning: Recommendation history", stderr.getvalue())


class SelectionArgumentTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [
            make_candidate("One", website="https://example.invalid/one"),
            make_candidate("Two", website="https://example.invalid/two"),
            make_candidate("Three", website="https://example.invalid/three"),
            make_candidate("Extra", website="https://example.invalid/extra"),
        ]
        self.identifiers = [candidate_id(candidate) for candidate in self.candidates]

    def test_extra_id_is_accepted_at_every_position_and_normal_order_is_preserved(self):
        normal_ids = [
            self.identifiers[2],
            self.identifiers[0],
            self.identifiers[1],
        ]
        extra_id = self.identifiers[3]

        for extra_position in range(4):
            with self.subTest(extra_position=extra_position):
                parts = list(normal_ids)
                parts.insert(extra_position, f"{EXTRA_ID_PREFIX}{extra_id}")
                selection_argument = ", ".join(parts)

                parsed_normal_ids, parsed_extra_id = parse_selection_argument(
                    selection_argument
                )
                resolved = resolve_selection(selection_argument, self.candidates)

                self.assertEqual(parsed_normal_ids, tuple(normal_ids))
                self.assertEqual(parsed_extra_id, extra_id)
                self.assertEqual(
                    [candidate["title"] for candidate in resolved.recommendations],
                    ["Three", "One", "Two"],
                )
                self.assertEqual(resolved.extra_recommendation["title"], "Extra")

    def test_parser_rejects_every_selection_argument_validation_error(self):
        one, two, three, extra = self.identifiers
        cases = [
            (
                "wrong number of IDs",
                f"{one},{two},x{extra}",
                "exactly four",
            ),
            (
                "empty ID",
                f"{one},,{two},x{extra}",
                "empty candidate ID",
            ),
            (
                "no extra ID",
                f"{one},{two},{three},{extra}",
                "exactly one extra",
            ),
            (
                "two extra IDs",
                f"x{one},x{two},{three},{extra}",
                "exactly one extra",
            ),
            (
                "uppercase extra prefix",
                f"{one},{two},{three},X{extra}",
                "exactly one extra",
            ),
            (
                "short hash",
                f"{one[:-1]},{two},{three},x{extra}",
                "complete lowercase SHA-256",
            ),
            (
                "uppercase hash",
                f"{one.upper()},{two},{three},x{extra}",
                "complete lowercase SHA-256",
            ),
            (
                "malformed extra hash",
                f"{one},{two},{three},x{'a' * 63}",
                "complete lowercase SHA-256",
            ),
            (
                "duplicate normal ID",
                f"{one},{one},{three},x{extra}",
                "duplicate candidate ID",
            ),
            (
                "extra duplicates a normal ID",
                f"{one},{two},{three},x{three}",
                "duplicate candidate ID",
            ),
        ]

        for name, selection_argument, expected_error in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(SelectionError, expected_error):
                    parse_selection_argument(selection_argument)

    def test_parser_defensively_rejects_an_unexpected_normal_id_count(self):
        one, two, three, extra = self.identifiers
        selection_argument = f"{one},{two},{three},x{extra}"

        with patch.object(selection, "NORMAL_RECOMMENDATION_COUNT", 2):
            with self.assertRaisesRegex(SelectionError, "exactly 2 normal"):
                parse_selection_argument(selection_argument)

    def test_resolver_rejects_unknown_but_well_formed_id(self):
        one, two, _three, extra = self.identifiers
        unknown = candidate_id(make_candidate("Not in this fetch"))
        self.assertNotIn(unknown, self.identifiers)
        selection_argument = f"{one},{two},{unknown},x{extra}"

        with self.assertRaisesRegex(SelectionError, "Unknown candidate ID"):
            resolve_selection(selection_argument, self.candidates)

    def test_resolver_accepts_repeated_rows_for_one_logical_candidate(self):
        candidates = [
            self.candidates[0],
            dict(self.candidates[0], description="Updated source synopsis."),
            *self.candidates[1:],
        ]
        one, two, three, extra = self.identifiers

        resolved = resolve_selection(
            f"{one},{two},{three},x{extra}",
            candidates,
        )

        self.assertEqual(
            [candidate["title"] for candidate in resolved.recommendations],
            ["One", "Two", "Three"],
        )
        self.assertIs(resolved.recommendations[0], self.candidates[0])
        self.assertEqual(resolved.extra_recommendation["title"], "Extra")


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [
            make_candidate(
                "One",
                channel="ARD",
                duration="00:42:00",
                description="First source description.",
                website="https://example.invalid/one",
            ),
            make_candidate(
                "Two",
                channel="ZDF",
                duration="01:05:00",
                description="Second source description.",
                website="https://example.invalid/two",
            ),
            make_candidate(
                "Three",
                channel="ARTE.DE",
                duration="00:50:00",
                description="Third source description.",
                website="https://example.invalid/three",
            ),
            make_candidate(
                "Extra",
                channel="ARD",
                duration="00:58:00",
                description="Extra source description.",
                website="https://example.invalid/extra",
            ),
        ]
        identifiers = [candidate_id(candidate) for candidate in self.candidates]
        self.selection = resolve_selection(
            f"{identifiers[2]},{identifiers[0]},{identifiers[1]},x{identifiers[3]}",
            self.candidates,
        )

    def test_renderer_owns_complete_metadata_url_and_extra_presentation(self):
        output = render_recommendations(self.selection, today=date(2026, 8, 12))

        self.assertIn("# 📺 DokuTipps der Woche – 2026-08-12", output)
        self.assertIn("## Empfehlungen", output)
        self.assertIn("### 1. 🎬 Three", output)
        self.assertIn("### 2. 🎬 One", output)
        self.assertIn("### 3. 🎬 Two", output)
        self.assertIn("## 🔭 Extra-Empfehlung", output)
        self.assertIn("### 🎬 Extra", output)
        self.assertNotIn("### 4.", output)

        expected_source_data = [
            ("ARTE.DE", "50 Min.", "Third source description.", "three"),
            ("ARD", "42 Min.", "First source description.", "one"),
            ("ZDF", "1 Std. 5 Min.", "Second source description.", "two"),
            ("ARD", "58 Min.", "Extra source description.", "extra"),
        ]
        for channel, duration, description, url_suffix in expected_source_data:
            with self.subTest(url_suffix=url_suffix):
                self.assertIn(f"📡 Sender: {channel}", output)
                self.assertIn(f"⏱ Laufzeit: {duration}", output)
                self.assertIn("📅 Datum: 11.08.2026", output)
                self.assertIn(description, output)
                self.assertIn(
                    f"[Zur Mediathek](https://example.invalid/{url_suffix})", output
                )

        positions = [
            output.index("Third source description."),
            output.index("First source description."),
            output.index("Second source description."),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_renderer_uses_deterministic_fallbacks_and_omits_absent_urls(self):
        candidates = [
            make_candidate(
                "",
                channel="",
                date="",
                duration="",
                description="",
                website="",
            ),
            make_candidate("No URL two", website=""),
            make_candidate("No URL three", website=""),
            make_candidate("No URL extra", website=""),
        ]
        identifiers = [candidate_id(candidate) for candidate in candidates]
        resolved = resolve_selection(
            f"{identifiers[0]},{identifiers[1]},{identifiers[2]},x{identifiers[3]}",
            candidates,
        )

        output = render_recommendations(resolved, today=date(2026, 8, 12))

        self.assertIn("### 1. 🎬 Ohne Titel", output)
        self.assertIn("📡 Sender: unbekannt", output)
        self.assertIn("⏱ Laufzeit: unbekannt", output)
        self.assertIn("📅 Datum: unbekannt", output)
        self.assertIn("Keine Beschreibung verfügbar.", output)
        self.assertNotIn("[Zur Mediathek]", output)

    def test_duration_formatting_is_deterministic(self):
        self.assertEqual(format_duration("01:05:00"), "1 Std. 5 Min.")
        self.assertEqual(format_duration("00:42:00"), "42 Min.")
        self.assertEqual(format_duration("00:00:05"), "5 Sek.")
        self.assertEqual(format_duration("00:00:00"), "0 Min.")
        self.assertEqual(format_duration("not-a-duration"), "not-a-duration")
        self.assertEqual(format_duration(""), "unbekannt")


class CacheTests(unittest.TestCase):
    def test_needs_download_respects_the_cache_age_threshold(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache = Path(temporary_directory) / cli.FILMLISTE_FILENAME
            self.assertTrue(filmliste.needs_download(cache))
            cache.touch()
            modified_at = cache.stat().st_mtime

            self.assertFalse(
                filmliste.needs_download(cache, now=modified_at + 1)
            )
            self.assertTrue(
                filmliste.needs_download(
                    cache,
                    now=modified_at + filmliste.MAX_AGE_SECONDS + 1,
                )
            )

    def test_ensure_filmliste_uses_fresh_cache_without_curl(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory) / "data"
            data_dir.mkdir()
            cache = data_dir / filmliste.FILMLISTE_FILENAME
            cache.write_bytes(lzma.compress(b"{}"))
            messages = []
            with patch.object(filmliste.subprocess, "run") as curl:
                result = filmliste.ensure_filmliste(data_dir, log=messages.append)

        self.assertEqual(result, cache)
        curl.assert_not_called()
        self.assertTrue(any("Filmliste-akt.xz is fresh" in item for item in messages))

    def test_ensure_filmliste_downloads_and_atomically_installs_a_valid_cache(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory) / "data"
            expected_cache = data_dir / filmliste.FILMLISTE_FILENAME
            messages = []

            def download(command):
                Path(command[3]).write_bytes(lzma.compress(b'{"X": []}'))
                return Mock(returncode=0)

            with patch.object(filmliste.subprocess, "run", side_effect=download) as curl:
                result = filmliste.ensure_filmliste(data_dir, log=messages.append)
            installed_data = lzma.decompress(expected_cache.read_bytes())

        self.assertEqual(result, expected_cache)
        self.assertEqual(installed_data, b'{"X": []}')
        command = curl.call_args.args[0]
        self.assertEqual(command[:3], ["curl", "-fsSL", "-o"])
        self.assertEqual(command[4], filmliste.DOWNLOAD_URL)
        self.assertNotEqual(Path(command[3]), expected_cache)
        self.assertTrue(any("Downloading Filmliste-akt.xz" in item for item in messages))
        self.assertIn("Download complete.", messages)

    def test_failed_refresh_uses_readable_stale_cache_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory) / "data"
            data_dir.mkdir()
            cache = data_dir / filmliste.FILMLISTE_FILENAME
            original = lzma.compress(b"stale but readable")
            cache.write_bytes(original)
            old_time = time.time() - filmliste.MAX_AGE_SECONDS - 60
            os.utime(cache, (old_time, old_time))
            messages = []
            with patch.object(
                filmliste.subprocess, "run", return_value=Mock(returncode=1)
            ):
                result = filmliste.ensure_filmliste(
                    data_dir,
                    allow_stale=True,
                    validate_existing=True,
                    log=messages.append,
                )
            result_bytes = cache.read_bytes()

        self.assertEqual(result, cache)
        self.assertEqual(result_bytes, original)
        self.assertTrue(any("using the existing" in item for item in messages))

    def test_cli_reports_a_failed_download_when_no_cache_exists(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            stderr = io.StringIO()
            with patch.object(
                cli,
                "prepare_filmliste",
                side_effect=filmliste.FilmlisteError("Download failed."),
            ), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as error:
                    cli.ensure_filmliste(Path(temporary_directory) / "data")

        self.assertEqual(error.exception.code, 1)
        self.assertIn("Download failed", stderr.getvalue())

    def test_setup_mode_fails_without_leaving_a_cache_when_no_fallback_exists(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory) / "data"
            with patch.object(
                filmliste.subprocess, "run", return_value=Mock(returncode=1)
            ):
                with self.assertRaises(filmliste.FilmlisteError):
                    filmliste.ensure_filmliste(
                        data_dir,
                        allow_stale=True,
                        validate_existing=True,
                    )

            self.assertFalse((data_dir / filmliste.FILMLISTE_FILENAME).exists())
            self.assertEqual(list(data_dir.glob("*.tmp")), [])


class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.history_file = Path(temporary_directory.name) / "recommendation-history.json"
        config_patch = patch.object(
            cli,
            "load_config",
            return_value=onboarding.AppConfig(skill_root=Path("/tmp/dokutipp-skills")),
        )
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.candidates = [
            make_candidate("One", website="https://example.invalid/one"),
            make_candidate("Two", website="https://example.invalid/two"),
            make_candidate("Three", website="https://example.invalid/three"),
            make_candidate("Extra", website="https://example.invalid/extra"),
        ]

    def test_configured_bare_cli_prints_help_to_stderr_and_exits_with_status_two(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(cli, "ensure_installation", return_value=False), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as error:
                cli.main([])

        self.assertEqual(error.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("usage:", stderr.getvalue())
        self.assertIn("fetch", stderr.getvalue())
        self.assertIn("select", stderr.getvalue())

    def test_version_does_not_require_onboarding(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(cli, "ensure_installation") as ensure, redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as error:
                cli.main(["--version"])

        self.assertEqual(error.exception.code, 0)
        self.assertEqual(stdout.getvalue(), "dokutipp 2.1.0\n")
        self.assertEqual(stderr.getvalue(), "")
        ensure.assert_not_called()

    def test_load_candidates_reuses_the_existing_parser_filters(self):
        filmliste = Path("/tmp/Filmliste-akt.xz")
        filter_file = Path("/tmp/filters.txt")
        with patch.object(cli, "ensure_filmliste", return_value=filmliste):
            with patch.object(cli, "parse_filmliste", return_value=self.candidates) as parse:
                result = cli.load_candidates(
                    data_dir=Path("/tmp/data"),
                    min_duration=90,
                    excluded_channels=("WDR",),
                    filter_file=filter_file,
                )

        self.assertIs(result, self.candidates)
        parse.assert_called_once_with(
            filmliste,
            min_duration=90,
            excluded_channels=("WDR",),
            filter_file=filter_file,
        )

    def test_cli_reloads_personal_sender_file_for_fetch_and_select(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sender_filter = root / "senders.txt"
            sender_filter.write_text("KiKA\n", encoding="utf-8")
            app_config = onboarding.AppConfig(
                skill_root=root / "skills",
                sender_filter_file=sender_filter,
            )
            identifiers = [candidate_id(candidate) for candidate in self.candidates]
            selection_argument = (
                f"{identifiers[0]},{identifiers[1]},{identifiers[2]},"
                f"x{identifiers[3]}"
            )
            fetch_output = io.StringIO()
            select_output = io.StringIO()

            with patch.object(cli, "ensure_installation", return_value=False), patch.object(
                cli, "load_config", return_value=app_config
            ), patch.object(
                cli, "load_candidates", return_value=self.candidates
            ) as load, redirect_stdout(fetch_output):
                cli.main(
                    ["fetch"],
                    history_file=self.history_file,
                    history_now=1_800_000_000.0,
                )
                sender_filter.write_text("ZDF\n", encoding="utf-8")
                with redirect_stdout(select_output):
                    cli.main(
                        ["select", selection_argument],
                        history_file=self.history_file,
                        history_now=1_800_000_000.0,
                    )

        self.assertEqual(load.call_args_list[0].kwargs["excluded_channels"], ("KiKA",))
        self.assertEqual(load.call_args_list[1].kwargs["excluded_channels"], ("ZDF",))
        self.assertTrue(select_output.getvalue().startswith("# 📺 DokuTipps der Woche"))

    def test_successful_select_records_all_four_ids_including_the_extra(self):
        selected_at = 1_800_000_000.0
        identifiers = [candidate_id(candidate) for candidate in self.candidates]
        selection_argument = (
            f"{identifiers[2]},x{identifiers[3]},"
            f"{identifiers[0]},{identifiers[1]}"
        )
        output = io.StringIO()

        with patch.object(cli, "load_candidates", return_value=self.candidates):
            cli.run_select(
                selection_argument,
                history_file=self.history_file,
                history_now=selected_at,
                output=output,
                today=date(2026, 8, 12),
            )

        self.assertTrue(output.getvalue().startswith("# 📺 DokuTipps der Woche"))
        self.assertEqual(
            set(history.load_recent_ids(self.history_file, now=selected_at)),
            set(identifiers),
        )

    def test_invalid_select_does_not_create_or_mutate_history(self):
        selected_at = 1_800_000_000.0
        existing_identifier = "f" * 64
        history.record_selected_ids(
            self.history_file,
            [existing_identifier],
            now=selected_at,
        )
        history_before = self.history_file.read_bytes()

        with patch.object(cli, "load_candidates", return_value=self.candidates):
            with self.assertRaisesRegex(SelectionError, "exactly four"):
                cli.run_select(
                    "not-a-valid-selection",
                    history_file=self.history_file,
                    history_now=selected_at + 60,
                    output=io.StringIO(),
                )

        self.assertEqual(self.history_file.read_bytes(), history_before)

    def test_select_history_write_failure_emits_no_output_and_preserves_history(self):
        identifiers = [candidate_id(candidate) for candidate in self.candidates]
        selection_argument = (
            f"{identifiers[0]},{identifiers[1]},{identifiers[2]},"
            f"x{identifiers[3]}"
        )
        existing_identifier = "f" * 64
        history.record_selected_ids(
            self.history_file,
            [existing_identifier],
            now=1_800_000_000.0,
        )
        history_before = self.history_file.read_bytes()
        output = io.StringIO()

        with patch.object(cli, "load_candidates", return_value=self.candidates), patch.object(
            cli,
            "record_selected_ids",
            side_effect=history.RecommendationHistoryError("history write failed"),
        ):
            with self.assertRaisesRegex(
                history.RecommendationHistoryError,
                "history write failed",
            ):
                cli.run_select(
                    selection_argument,
                    history_file=self.history_file,
                    history_now=1_800_000_060.0,
                    output=output,
                )

        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.history_file.read_bytes(), history_before)

    def test_cli_reports_a_history_write_failure_without_result_stdout(self):
        identifiers = [candidate_id(candidate) for candidate in self.candidates]
        selection_argument = (
            f"{identifiers[0]},{identifiers[1]},{identifiers[2]},"
            f"x{identifiers[3]}"
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(cli, "ensure_installation", return_value=False), patch.object(
            cli, "load_candidates", return_value=self.candidates
        ), patch.object(
            cli,
            "record_selected_ids",
            side_effect=history.RecommendationHistoryError("history write failed"),
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as error:
                cli.main(
                    ["select", selection_argument],
                    history_file=self.history_file,
                    history_now=1_800_000_000.0,
                )

        self.assertEqual(error.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Error: history write failed", stderr.getvalue())

    def test_fetch_then_select_end_to_end_uses_stdout_for_results_and_stderr_for_logs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory) / "data"
            data_dir.mkdir()
            filmliste = data_dir / cli.FILMLISTE_FILENAME
            now = int(time.time())
            entries = [
                make_entry(
                    "ARD",
                    "One",
                    "00:42:00",
                    now,
                    description="First source description.",
                    website="https://example.invalid/one",
                ),
                make_entry(
                    "ZDF",
                    "Two",
                    "01:05:00",
                    now,
                    description="Second source description.",
                    website="https://example.invalid/two",
                ),
                make_entry(
                    "ARTE.DE",
                    "Three",
                    "00:50:00",
                    now,
                    description="Third source description.",
                    website="https://example.invalid/three",
                ),
                make_entry(
                    "ARD",
                    "Extra",
                    "00:58:00",
                    now,
                    description="Extra source description.",
                    website="https://example.invalid/extra",
                ),
            ]
            write_filmliste(filmliste, entries)
            filter_file = data_dir / "filters.txt"
            filter_file.write_text(
                "# No additional exclusions for this test\n",
                encoding="utf-8",
            )

            fetch_stdout = io.StringIO()
            fetch_stderr = io.StringIO()
            with patch.object(
                cli, "ensure_installation", return_value=False
            ), redirect_stdout(fetch_stdout), redirect_stderr(fetch_stderr):
                cli.main(
                    [
                        "fetch",
                        "--limit",
                        "4",
                        "--min-duration",
                        "42",
                        "--filter-file",
                        str(filter_file),
                    ],
                    data_dir=data_dir,
                    history_file=self.history_file,
                    history_now=1_800_000_000.0,
                )

            fetch_payload = json.loads(fetch_stdout.getvalue())
            candidate_ids_by_title = {
                candidate["title"]: candidate["id"]
                for candidate in fetch_payload["candidates"]
            }
            selection_argument = ",".join(
                [
                    candidate_ids_by_title["Three"],
                    f"{EXTRA_ID_PREFIX}{candidate_ids_by_title['Extra']}",
                    candidate_ids_by_title["One"],
                    candidate_ids_by_title["Two"],
                ]
            )

            select_stdout = io.StringIO()
            select_stderr = io.StringIO()
            with patch.object(
                cli, "ensure_installation", return_value=False
            ), redirect_stdout(select_stdout), redirect_stderr(select_stderr):
                cli.main(
                    [
                        "select",
                        selection_argument,
                        "--limit",
                        "4",
                        "--min-duration",
                        "42",
                        "--filter-file",
                        str(filter_file),
                    ],
                    data_dir=data_dir,
                    history_file=self.history_file,
                    history_now=1_800_000_000.0,
                )

            repeated_fetch_stdout = io.StringIO()
            repeated_fetch_stderr = io.StringIO()
            with patch.object(
                cli, "ensure_installation", return_value=False
            ), redirect_stdout(repeated_fetch_stdout), redirect_stderr(
                repeated_fetch_stderr
            ):
                cli.main(
                    [
                        "fetch",
                        "--limit",
                        "4",
                        "--min-duration",
                        "42",
                        "--filter-file",
                        str(filter_file),
                    ],
                    data_dir=data_dir,
                    history_file=self.history_file,
                    history_now=1_800_000_000.0,
                )
            repeated_fetch_payload = json.loads(repeated_fetch_stdout.getvalue())

        self.assertEqual(fetch_payload["status"], "ready")
        self.assertNotIn("https://example.invalid/one", fetch_stdout.getvalue())
        self.assertNotIn("Filmliste-akt.xz is fresh", fetch_stdout.getvalue())
        self.assertIn("Filmliste-akt.xz is fresh", fetch_stderr.getvalue())
        self.assertTrue(select_stdout.getvalue().startswith("# 📺 DokuTipps der Woche"))
        self.assertNotIn('"status"', select_stdout.getvalue())
        self.assertNotIn("Filmliste-akt.xz is fresh", select_stdout.getvalue())
        self.assertIn("Filmliste-akt.xz is fresh", select_stderr.getvalue())
        self.assertLess(
            select_stdout.getvalue().index("### 1. 🎬 Three"),
            select_stdout.getvalue().index("### 2. 🎬 One"),
        )
        self.assertLess(
            select_stdout.getvalue().index("### 2. 🎬 One"),
            select_stdout.getvalue().index("### 3. 🎬 Two"),
        )
        self.assertIn("## 🔭 Extra-Empfehlung", select_stdout.getvalue())
        self.assertIn(
            "[Zur Mediathek](https://example.invalid/three)", select_stdout.getvalue()
        )
        self.assertIn(
            "[Zur Mediathek](https://example.invalid/extra)", select_stdout.getvalue()
        )
        self.assertEqual(repeated_fetch_payload["status"], "no_candidates")
        self.assertEqual(repeated_fetch_payload["candidates"], [])

    def test_select_validation_failure_writes_only_a_stderr_error(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(cli, "load_candidates", return_value=self.candidates), patch.object(
            cli, "ensure_installation", return_value=False
        ):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as error:
                    cli.main(
                        ["select", "not-a-valid-selection"],
                        history_file=self.history_file,
                    )

        self.assertEqual(error.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Error: Selection must contain exactly four", stderr.getvalue())

    def test_invalid_filter_file_is_reported_as_cli_error(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            filter_file = Path(temporary_directory) / "filters.txt"
            filter_file.write_text("[unterminated", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()

            with patch.object(cli, "load_candidates", return_value=[]), patch.object(
                cli, "ensure_installation", return_value=False
            ):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as error:
                        cli.main(
                            ["fetch", "--filter-file", str(filter_file)],
                            history_file=self.history_file,
                        )

        self.assertEqual(error.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Invalid title filter", stderr.getvalue())

    def test_explicit_filter_file_is_not_exposed_in_fetch_payload(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            filter_file = Path(temporary_directory) / "filters.txt"
            filter_file.write_text(
                "# Ignore comments and blank lines\n\nMittags[- ]magazin\n",
                encoding="utf-8",
            )
            output = io.StringIO()

            with patch.object(cli, "load_candidates", return_value=self.candidates):
                payload = cli.run_fetch(
                    filter_file=filter_file,
                    history_file=self.history_file,
                    output=output,
                )

        self.assertNotIn("filters", payload)

    def test_select_workflow_does_not_read_skill_md(self):
        identifiers = [candidate_id(candidate) for candidate in self.candidates]
        selection_argument = (
            f"{identifiers[0]},{identifiers[1]},{identifiers[2]},"
            f"x{identifiers[3]}"
        )
        output = io.StringIO()
        original_read_text = Path.read_text

        def read_text_except_skill(path, *args, **kwargs):
            if path.name == "SKILL.md":
                raise AssertionError("SKILL.md must not be read")
            return original_read_text(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary_directory:
            original_cwd = Path.cwd()
            try:
                os.chdir(temporary_directory)
                with patch.object(cli, "load_candidates", return_value=self.candidates):
                    with patch.object(
                        Path,
                        "read_text",
                        autospec=True,
                        side_effect=read_text_except_skill,
                    ):
                        cli.run_select(
                            selection_argument,
                            history_file=self.history_file,
                            output=output,
                            today=date(2026, 8, 12),
                        )
            finally:
                os.chdir(original_cwd)

        self.assertIn("## 🔭 Extra-Empfehlung", output.getvalue())

    def test_parser_does_not_prefer_a_sender_by_default(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            filmliste = Path(temporary_directory) / cli.FILMLISTE_FILENAME
            now = int(time.time())
            entries = [
                make_entry("ARD", "Eligible documentary", "00:42:00", now),
                make_entry("", "Too short", "00:41:00", now),
                make_entry("ZDF", "Audiodeskription version", "00:50:00", now),
                make_entry("ARD", "Mittagsmagazin", "00:50:00", now),
                make_entry("ARTE.DE", "Too old", "00:50:00", now - 8 * 24 * 3600),
                make_entry("WDR", "Wrong channel", "00:50:00", now),
            ]
            write_filmliste(filmliste, entries)

            results = parser.parse_filmliste(
                filmliste,
                limit=cli.DEFAULT_LIMIT,
                min_duration=cli.DEFAULT_MIN_DURATION,
                excluded_channels=(),
            )

        self.assertEqual(
            [entry["title"] for entry in results],
            ["Eligible documentary", "Wrong channel"],
        )

    def test_parser_excludes_senders_when_explicitly_requested(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            filmliste = Path(temporary_directory) / cli.FILMLISTE_FILENAME
            now = int(time.time())
            entries = [
                make_entry("ARD", "ARD documentary", "00:42:00", now),
                make_entry("WDR", "WDR documentary", "00:42:00", now),
            ]
            write_filmliste(filmliste, entries)

            results = parser.parse_filmliste(
                filmliste,
                excluded_channels=("wdr",),
            )

        self.assertEqual([entry["title"] for entry in results], ["ARD documentary"])

    def test_sender_exclusion_applies_after_delta_decoding_and_ignores_case(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache = Path(temporary_directory) / cli.FILMLISTE_FILENAME
            now = int(time.time())
            write_filmliste(
                cache,
                [
                    make_entry("RBTV", "First RBTV documentary", "00:42:00", now),
                    make_entry("", "Inherited RBTV documentary", "00:42:00", now),
                    make_entry("WDR", "WDR documentary", "00:42:00", now),
                ],
            )

            results = parser.parse_filmliste(
                cache,
                excluded_channels=("rbtv",),
            )

        self.assertEqual([entry["title"] for entry in results], ["WDR documentary"])

    def test_sender_exclusions_are_literal_not_regular_expressions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache = Path(temporary_directory) / cli.FILMLISTE_FILENAME
            now = int(time.time())
            write_filmliste(
                cache,
                [
                    make_entry("ZDFneo", "ZDFneo documentary", "00:42:00", now),
                    make_entry("ZDF.*", "Literal-name documentary", "00:42:00", now),
                ],
            )

            results = parser.parse_filmliste(
                cache,
                excluded_channels=("ZDF.*",),
            )

        self.assertEqual([entry["title"] for entry in results], ["ZDFneo documentary"])

    def test_available_channels_uses_all_raw_rows_and_casefolds_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache = Path(temporary_directory) / cli.FILMLISTE_FILENAME
            write_filmliste(
                cache,
                [
                    make_entry("rbtv", "Old", "00:01:00", 1),
                    make_entry("", "Inherited", "00:01:00", 1),
                    make_entry("RBTV", "Variant", "00:01:00", 1),
                    make_entry("ARD", "Future", "00:01:00", 9_999_999_999),
                ],
            )

            channels = parser.available_channels(cache)

        self.assertEqual(channels, ("ARD", "rbtv"))

    def test_default_title_filters_exclude_formats_but_preserve_documentaries(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            filmliste = Path(temporary_directory) / cli.FILMLISTE_FILENAME
            now = int(time.time())
            excluded_titles = [
                "Volle Kanne vom 14. August 2026",
                "Wakefield (1/8)",
                "Leichtathletik-EM: Frühsession vom 13. August",
                "Trailer: Neue Serie",
                "Brisant vom 10. August",
                "Die Frankenschau vom 09.08.2026",
                "Umschau_ MDR-Magazin vom 11. August",
                "Markt - die ganze Sendung | 10.08.2026 (Gebärdensprache)",
                "Visite | 11.08.2026 (Gebärdensprache)",
                "Das große Hessenquiz vom 20.10.2024",
                "BINGO! | 09.08.2026",
                "Das Fifty-Fifty Quiz | 09.08.2026",
                "Kaum zu glauben! | 09.08.2026",
                "Inas Nacht mit Jan Ullrich und Özcan Coşar",
                "STÖCKL vom 13.08.2026",
                (
                    "Sommergespräche 2026: Leonore Gewessler (Die Grünen) "
                    "im Gespräch mit Simone Stribl (ÖGS)"
                ),
                "Sommer(nach)gespräche: Die Grünen",
                "Sommer-Spaß mit Andy Borg 2026",
                "Vorstadtweiber (1/10)",
                "In der Tiefe – Maria Wern, Kripo Gotland (S07/E02)",
                "Spuren des Bösen: Zauberberg",
                "Blind ermittelt: Tod im Prater",
                "Tag für Tag – Hotel Heidelberg",
                "Kommen und Gehen - Hotel Heidelberg",
                "SWR Sport mit KSC-Trainer Maximilian Senft",
                "Sportclub live - 3. Liga: Rot-Weiss Essen - TSV Havelse",
                "Champions League Quali: SK Sturm Graz - Fenerbahçe Istanbul",
                (
                    "Fußball 2. Liga: Blau-Weiß Linz - Wacker Innsbruck, "
                    "Highlights aus Linz"
                ),
                (
                    "Fußball Frauen Bundesliga: LASK - Sturm Graz, "
                    "Highlights aus Linz"
                ),
            ]
            preserved_titles = [
                "ARTE Reportage - Russland / Madagaskar",
                "Stadt Land Kunst - Japan / El Salvador / Delphi",
                "Studio 54 - Hinter den Pforten des legendären Clubs",
                "Folge 8: Doppelalarm für Christoph 9 (S11/E08)",
                "Undercover in Saudi-Arabien (S02/E01)",
                "Weltspiegel vom 9.8.2026",
            ]
            entries = [
                make_entry("ARTE.DE", title, "00:50:00", now)
                for title in excluded_titles + preserved_titles
            ]
            write_filmliste(filmliste, entries)

            results = parser.parse_filmliste(
                filmliste,
                min_duration=cli.DEFAULT_MIN_DURATION,
                excluded_channels=(),
            )

        self.assertEqual(
            [entry["title"] for entry in results],
            preserved_titles,
        )

    def test_parser_uses_case_insensitive_title_regexes_from_filter_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            filmliste = Path(temporary_directory) / cli.FILMLISTE_FILENAME
            filter_file = Path(temporary_directory) / "filters.txt"
            now = int(time.time())
            filter_file.write_text(
                "# Title exclusions\n\nMittags[- ]?magazin\nEvening\\s+Report\n",
                encoding="utf-8",
            )
            entries = [
                make_entry(
                    "ARD",
                    "Documentary with context",
                    "00:10:00",
                    now,
                    description="A Mittagsmagazin mention is only in the description.",
                ),
                make_entry("ZDF", "MITTAGSMAGAZIN Spezial", "00:10:00", now),
                make_entry("ARD", "Evening Report: Europe", "00:10:00", now),
                make_entry("ARTE.DE", "Audiodeskription version", "00:10:00", now),
            ]
            write_filmliste(filmliste, entries)

            results = parser.parse_filmliste(
                filmliste,
                min_duration=0,
                excluded_channels=(),
                filter_file=filter_file,
            )

        self.assertEqual(
            [entry["title"] for entry in results],
            ["Documentary with context", "Audiodeskription version"],
        )

    def test_filter_file_ignores_blank_and_comment_lines(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            filter_file = Path(temporary_directory) / "filters.txt"
            filter_file.write_text("\n  # comment\nMittagsmagazin\n", encoding="utf-8")

            self.assertEqual(
                parser.load_title_filters(filter_file),
                ("Mittagsmagazin",),
            )

    def test_sender_filter_file_uses_literal_casefolded_unique_lines(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            filter_file = Path(temporary_directory) / "senders.txt"
            filter_file.write_text(
                "\n# personal exclusions\nKiKA\nkika\nZDF.*\n",
                encoding="utf-8",
            )

            self.assertEqual(
                parser.load_sender_filters(filter_file),
                ("KiKA", "ZDF.*"),
            )

    def test_direct_parser_replaces_channels_with_sender_filter_file(self):
        argument_parser = parser.build_argument_parser()

        arguments = argument_parser.parse_args(
            ["cache.xz", "--sender-filter-file", "senders.txt"]
        )

        self.assertEqual(arguments.sender_filter_file, Path("senders.txt"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            argument_parser.parse_args(["cache.xz", "--channels", "ARD"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_argument_parser().parse_args(["fetch", "--channels", "ARD"])

    def test_invalid_filter_regex_includes_file_and_line(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            filter_file = Path(temporary_directory) / "filters.txt"
            filter_file.write_text("Valid\n[invalid", encoding="utf-8")

            with self.assertRaisesRegex(
                FilterConfigError,
                r"Invalid title filter.*filters\.txt.*line 2",
            ):
                parser.load_title_filters(filter_file)

    def test_missing_filter_file_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            filter_file = Path(temporary_directory) / "missing-filters.txt"

            with self.assertRaisesRegex(
                FilterConfigError, "Title filter file not found"
            ):
                parser.load_title_filters(filter_file)


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.canonical_skill = REPOSITORY_ROOT / "SKILL.md"
        self.catalog_patch = patch.object(
            onboarding,
            "_setup_sender_catalog",
            return_value=("ARD", "KiKA", "ZDF"),
        )
        self.selector_patch = patch.object(
            onboarding,
            "_questionary_sender_selector",
            side_effect=lambda _channels, default_allowed, **_kwargs: default_allowed,
        )
        self.catalog_patch.start()
        self.selector_patch.start()
        self.addCleanup(self.catalog_patch.stop)
        self.addCleanup(self.selector_patch.stop)

    def write_config(self, config_file, skill_root):
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps({"agent": "hermes", "skill_root": str(skill_root)}) + "\n",
            encoding="utf-8",
        )

    def write_complete_installation(self, skill_root):
        skill_directory = skill_root / "dokutipp"
        skill_directory.mkdir(parents=True)
        (skill_directory / "SKILL.md").write_bytes(self.canonical_skill.read_bytes())
        (skill_directory / "PROFILE.md").write_text(
            "# Personal Profile\n\n## Interests\n\nHistory\n",
            encoding="utf-8",
        )
        return skill_directory

    def read_skill_frontmatter(self):
        content = self.canonical_skill.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("---\n"))
        _opening, frontmatter, body = content.split("---", 2)
        properties = {}
        top_level_keys = []
        for line in frontmatter.strip().splitlines():
            if not line.startswith((" ", "\t")):
                key, value = line.split(":", 1)
                top_level_keys.append(key)
                properties[key] = value.strip()
        self.assertTrue(body.strip())
        return properties, top_level_keys

    def test_canonical_skill_frontmatter_matches_agent_skills_standard(self):
        properties, top_level_keys = self.read_skill_frontmatter()
        allowed_fields = {
            "name",
            "description",
            "license",
            "compatibility",
            "metadata",
            "allowed-tools",
        }

        self.assertEqual(len(top_level_keys), len(set(top_level_keys)))
        self.assertLessEqual(set(top_level_keys), allowed_fields)
        self.assertIn("name", properties)
        self.assertIn("description", properties)
        self.assertRegex(properties["name"], r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        self.assertLessEqual(len(properties["name"]), 64)
        self.assertEqual(properties["name"], onboarding.SKILL_DIRECTORY_NAME)
        self.assertGreater(len(properties["description"]), 0)
        self.assertLessEqual(len(properties["description"]), 1024)
        self.assertIn("Use when", properties["description"])
        self.assertLessEqual(len(properties["compatibility"]), 500)
        self.assertLessEqual(
            len(self.canonical_skill.read_text(encoding="utf-8").splitlines()),
            500,
        )

    def test_first_bare_cli_run_installs_hermes_skill_and_profile(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hermes_home = root / "hermes-home"
            home = root / "home"
            config_file = home / ".dokutipp" / "config.json"
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout):
                cli.main(
                    [],
                    input_stream=InteractiveInput("history and science\n1\nsports\n"),
                    onboarding_output=stderr,
                    environment={"HERMES_HOME": str(hermes_home)},
                    home=home,
                )

            skill_directory = hermes_home / "skills" / "dokutipp"
            self.assertEqual(stdout.getvalue(), "")
            self.assertTrue((home / ".dokutipp" / "data").is_dir())
            self.assertFalse((home / ".dokutipp" / "filters.txt").exists())
            sender_filter_file = home / ".dokutipp" / "senders.txt"
            self.assertTrue(sender_filter_file.is_file())
            self.assertEqual(sender_filter_file.read_text(encoding="utf-8"), "")
            self.assertEqual(
                json.loads(config_file.read_text(encoding="utf-8")),
                {
                    "agent": "hermes",
                    "skill_root": str((hermes_home / "skills").resolve()),
                    "sender_filter_file": str(sender_filter_file.resolve()),
                },
            )
            self.assertEqual(
                (skill_directory / "SKILL.md").read_bytes(),
                self.canonical_skill.read_bytes(),
            )
            profile = (skill_directory / "PROFILE.md").read_text(encoding="utf-8")
            self.assertIn("history and science", profile)
            self.assertIn("sports", profile)
            self.assertIn("comma-separated on one line", stderr.getvalue())
            self.assertIn("DokuTipp setup is complete", stderr.getvalue())

            subsequent_stdout = io.StringIO()
            subsequent_stderr = io.StringIO()
            with redirect_stdout(subsequent_stdout), redirect_stderr(subsequent_stderr):
                with self.assertRaises(SystemExit) as error:
                    cli.main(
                        [],
                        input_stream=io.StringIO(),
                        environment={"HERMES_HOME": str(hermes_home)},
                        home=home,
                    )
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(subsequent_stdout.getvalue(), "")
            self.assertIn("usage:", subsequent_stderr.getvalue())

    def test_new_onboarding_does_not_read_or_modify_legacy_config_or_data(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            hermes_home = root / "hermes-home"
            legacy_config = root / "xdg" / "dokutipp" / "config.json"
            legacy_skill_root = root / "legacy-skills"
            self.write_config(legacy_config, legacy_skill_root)
            legacy_data = root / "data"
            legacy_data.mkdir()
            legacy_cache = legacy_data / cli.FILMLISTE_FILENAME
            legacy_cache.write_bytes(b"legacy-cache")

            cli.main(
                [],
                input_stream=InteractiveInput("history\n1\n\n"),
                onboarding_output=io.StringIO(),
                environment={
                    "XDG_CONFIG_HOME": str(root / "xdg"),
                    "HERMES_HOME": str(hermes_home),
                },
                home=home,
            )

            self.assertTrue((home / ".dokutipp" / "config.json").is_file())
            self.assertEqual(json.loads(legacy_config.read_text(encoding="utf-8"))["skill_root"], str(legacy_skill_root))
            self.assertEqual(legacy_cache.read_bytes(), b"legacy-cache")
            self.assertFalse((home / ".dokutipp" / "data" / cli.FILMLISTE_FILENAME).exists())

    def test_first_run_supports_a_manual_skill_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manual_root = root / "manual-skills"
            home = root / "home"
            config_file = home / ".dokutipp" / "config.json"

            cli.main(
                [],
                input_stream=InteractiveInput(
                    f"nature\n2\n{manual_root}\ncelebrity news\n"
                ),
                onboarding_output=io.StringIO(),
                home=home,
            )

            self.assertTrue((manual_root / "dokutipp" / "SKILL.md").is_file())
            self.assertEqual(
                json.loads(config_file.read_text(encoding="utf-8"))["skill_root"],
                str(manual_root.resolve()),
            )

    def test_config_supports_legacy_and_personal_sender_filter_forms(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_file = root / "config.json"
            skill_root = root / "skills"
            self.write_config(config_file, skill_root)

            legacy = onboarding.load_config(config_file)

            sender_filter = root / "personal" / "senders.txt"
            config_file.write_text(
                json.dumps(
                    {
                        "agent": "hermes",
                        "skill_root": str(skill_root),
                        "sender_filter_file": str(sender_filter),
                    }
                ),
                encoding="utf-8",
            )
            configured = onboarding.load_config(config_file)

            self.assertEqual(legacy, onboarding.AppConfig(skill_root=skill_root))
            self.assertEqual(
                configured,
                onboarding.AppConfig(
                    skill_root=skill_root,
                    sender_filter_file=sender_filter,
                ),
            )

            for invalid_value in (None, "", "relative/senders.txt", []):
                with self.subTest(invalid_value=invalid_value):
                    config_file.write_text(
                        json.dumps(
                            {
                                "agent": "hermes",
                                "skill_root": str(skill_root),
                                "sender_filter_file": invalid_value,
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaises(onboarding.OnboardingError):
                        onboarding.load_config(config_file)

    def test_preflight_recreates_a_configured_missing_sender_filter_empty(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skill_root = root / "skills"
            self.write_complete_installation(skill_root)
            sender_filter = root / "personal" / "senders.txt"
            config_file = root / "config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "agent": "hermes",
                        "skill_root": str(skill_root),
                        "sender_filter_file": str(sender_filter),
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()

            onboarding.ensure_installation(
                config_file=config_file,
                data_dir=root / "data",
                input_stream=io.StringIO(),
                output_stream=output,
                canonical_skill_file=self.canonical_skill,
            )
            saved_filter = sender_filter.read_text(encoding="utf-8")
            output_value = output.getvalue()

        self.assertEqual(saved_filter, "")
        self.assertIn("Created empty sender filter file", output_value)

    def test_preflight_refuses_a_symlinked_sender_filter(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skill_root = root / "skills"
            self.write_complete_installation(skill_root)
            target = root / "target.txt"
            target.write_text("KiKA\n", encoding="utf-8")
            sender_filter = root / "senders.txt"
            sender_filter.symlink_to(target)
            config_file = root / "config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "agent": "hermes",
                        "skill_root": str(skill_root),
                        "sender_filter_file": str(sender_filter),
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(onboarding.OnboardingError, "symbolic link"):
                onboarding.ensure_installation(
                    config_file=config_file,
                    data_dir=root / "data",
                    input_stream=io.StringIO(),
                    output_stream=io.StringIO(),
                    canonical_skill_file=self.canonical_skill,
                )

            self.assertEqual(target.read_text(encoding="utf-8"), "KiKA\n")

    def test_preflight_recreates_an_invalid_utf8_sender_filter_empty(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skill_root = root / "skills"
            self.write_complete_installation(skill_root)
            sender_filter = root / "senders.txt"
            sender_filter.write_bytes(b"\xff\xfe")
            config_file = root / "config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "agent": "hermes",
                        "skill_root": str(skill_root),
                        "sender_filter_file": str(sender_filter),
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()

            onboarding.ensure_installation(
                config_file=config_file,
                data_dir=root / "data",
                input_stream=io.StringIO(),
                output_stream=output,
                canonical_skill_file=self.canonical_skill,
            )

            self.assertEqual(sender_filter.read_text(encoding="utf-8"), "")
            self.assertIn("Recreated the sender filter file empty", output.getvalue())

    def test_setup_preserves_unknown_senders_and_writes_unchecked_choices(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skill_root = root / "skills"
            skill_directory = self.write_complete_installation(skill_root)
            sender_filter = root / "personal" / "senders.txt"
            sender_filter.parent.mkdir()
            sender_filter.write_text("KiKA\nRetired TV\n", encoding="utf-8")
            config_file = root / "config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "agent": "hermes",
                        "skill_root": str(skill_root),
                        "sender_filter_file": str(sender_filter),
                    }
                ),
                encoding="utf-8",
            )
            captured = {}

            def choose(channels, default_allowed):
                captured["channels"] = tuple(channels)
                captured["default_allowed"] = tuple(default_allowed)
                return ("ARD", "Retired TV")

            cli.main(
                ["setup"],
                config_file=config_file,
                data_dir=root / "data",
                home=root / "home",
                input_stream=InteractiveInput(
                    "science\n2\n" + str(skill_root) + "\nwar\nn\n"
                ),
                onboarding_output=io.StringIO(),
                canonical_skill_file=self.canonical_skill,
                sender_selector=choose,
            )

            saved_config = json.loads(config_file.read_text(encoding="utf-8"))
            saved_filters = sender_filter.read_text(encoding="utf-8").splitlines()
            profile_exists = (skill_directory / "PROFILE.md").is_file()

        self.assertEqual(
            captured["channels"],
            ("ARD", "KiKA", "Retired TV", "ZDF"),
        )
        self.assertEqual(captured["default_allowed"], ("ARD", "ZDF"))
        self.assertEqual(saved_filters, ["KiKA", "ZDF"])
        self.assertEqual(saved_config["sender_filter_file"], str(sender_filter))
        self.assertTrue(profile_exists)

    def test_cancelled_sender_selection_leaves_personal_files_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            config_file = home / ".dokutipp" / "config.json"
            skill_root = root / "skills"

            with self.assertRaisesRegex(onboarding.OnboardingError, "cancelled"):
                onboarding.run_setup(
                    config_file=config_file,
                    data_dir=home / ".dokutipp" / "data",
                    input_stream=InteractiveInput(
                        "history\n2\n" + str(skill_root) + "\n\n"
                    ),
                    output_stream=io.StringIO(),
                    canonical_skill_file=self.canonical_skill,
                    home=home,
                    sender_selector=lambda _channels, _default: None,
                )

            self.assertFalse(config_file.exists())
            self.assertFalse((home / ".dokutipp" / "senders.txt").exists())
            self.assertFalse((skill_root / "dokutipp").exists())

    def test_setup_sender_catalog_uses_a_fresh_cache_without_downloading(self):
        self.catalog_patch.stop()
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory) / "data"
            data_dir.mkdir()
            cache = data_dir / filmliste.FILMLISTE_FILENAME
            write_filmliste(
                cache,
                [
                    make_entry("ARD", "One", "00:01:00", 1),
                    make_entry("ZDF", "Two", "00:01:00", 1),
                ],
            )
            output = io.StringIO()

            with patch.object(filmliste.subprocess, "run") as download:
                channels = onboarding._setup_sender_catalog(data_dir, output)

        self.assertEqual(channels, ("ARD", "ZDF"))
        download.assert_not_called()
        self.assertIn("fresh, skipping download", output.getvalue())

    @unittest.skipUnless(
        importlib.util.find_spec("questionary") is not None,
        "questionary is installed with the project dependency",
    )
    def test_questionary_sender_selector_toggles_with_space_and_confirms_with_enter(self):
        self.selector_patch.stop()
        master_descriptor, slave_descriptor = pty.openpty()
        input_stream = os.fdopen(os.dup(slave_descriptor), "r", buffering=1)
        output_stream = os.fdopen(os.dup(slave_descriptor), "w", buffering=1)

        def send_keys():
            time.sleep(0.05)
            os.write(master_descriptor, b" ")
            time.sleep(0.05)
            os.write(master_descriptor, b"\r")

        key_sender = threading.Thread(target=send_keys, daemon=True)
        key_sender.start()
        try:
            selected = onboarding._questionary_sender_selector(
                ("ARD", "ZDF"),
                ("ARD", "ZDF"),
                input_stream=input_stream,
                output_stream=output_stream,
            )
            os.set_blocking(master_descriptor, False)
            transcript_parts = []
            while True:
                try:
                    transcript_parts.append(os.read(master_descriptor, 65_536))
                except BlockingIOError:
                    break
        finally:
            key_sender.join(timeout=1)
            input_stream.close()
            output_stream.close()
            os.close(master_descriptor)
            os.close(slave_descriptor)

        self.assertEqual(selected, ["ZDF"])
        transcript = b"".join(transcript_parts).decode("utf-8", errors="replace")
        self.assertIn("- [x] ARD", transcript)
        self.assertIn("- [ ] ARD", transcript)
        self.assertNotIn("\x1b[0;7m", transcript)

    def test_preflight_repairs_missing_files_and_handles_modified_skill(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skill_root = root / "skills"
            config_file = root / "config.json"
            skill_directory = self.write_complete_installation(skill_root)
            self.write_config(config_file, skill_root)

            (skill_directory / "SKILL.md").unlink()
            onboarding.ensure_installation(
                config_file=config_file,
                home=root / "home",
                input_stream=io.StringIO(),
                output_stream=io.StringIO(),
                canonical_skill_file=self.canonical_skill,
            )
            self.assertEqual(
                (skill_directory / "SKILL.md").read_bytes(),
                self.canonical_skill.read_bytes(),
            )
            data_dir = root / "home" / ".dokutipp" / "data"
            self.assertTrue(data_dir.is_dir())
            data_dir.rmdir()
            onboarding.ensure_installation(
                config_file=config_file,
                home=root / "home",
                input_stream=io.StringIO(),
                output_stream=io.StringIO(),
                canonical_skill_file=self.canonical_skill,
            )
            self.assertTrue(data_dir.is_dir())

            (skill_directory / "PROFILE.md").unlink()
            onboarding.ensure_installation(
                config_file=config_file,
                home=root / "home",
                input_stream=InteractiveInput("technology\n\n"),
                output_stream=io.StringIO(),
                canonical_skill_file=self.canonical_skill,
            )
            self.assertIn(
                "technology",
                (skill_directory / "PROFILE.md").read_text(encoding="utf-8"),
            )

            skill_file = skill_directory / "SKILL.md"
            skill_file.write_text("local change", encoding="utf-8")
            onboarding.ensure_installation(
                config_file=config_file,
                home=root / "home",
                input_stream=InteractiveInput("n\n"),
                output_stream=io.StringIO(),
                canonical_skill_file=self.canonical_skill,
            )
            self.assertEqual(skill_file.read_text(encoding="utf-8"), "local change")

            onboarding.ensure_installation(
                config_file=config_file,
                home=root / "home",
                input_stream=InteractiveInput("y\n"),
                output_stream=io.StringIO(),
                canonical_skill_file=self.canonical_skill,
            )
            self.assertEqual(skill_file.read_bytes(), self.canonical_skill.read_bytes())

    def test_preflight_requires_a_terminal_for_missing_profile_or_modified_skill(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skill_root = root / "skills"
            config_file = root / "config.json"
            skill_directory = self.write_complete_installation(skill_root)
            self.write_config(config_file, skill_root)

            (skill_directory / "PROFILE.md").unlink()
            with self.assertRaisesRegex(onboarding.OnboardingError, "interactive terminal"):
                onboarding.ensure_installation(
                    config_file=config_file,
                    home=root / "home",
                    input_stream=io.StringIO(),
                    output_stream=io.StringIO(),
                    canonical_skill_file=self.canonical_skill,
                )

            (skill_directory / "PROFILE.md").write_text("profile", encoding="utf-8")
            (skill_directory / "SKILL.md").write_text("local change", encoding="utf-8")
            with self.assertRaisesRegex(onboarding.OnboardingError, "interactive terminal"):
                onboarding.ensure_installation(
                    config_file=config_file,
                    home=root / "home",
                    input_stream=io.StringIO(),
                    output_stream=io.StringIO(),
                    canonical_skill_file=self.canonical_skill,
                )

    def test_preflight_refuses_symlinked_skill_or_profile_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skill_root = root / "skills"
            config_file = root / "config.json"
            skill_directory = self.write_complete_installation(skill_root)
            self.write_config(config_file, skill_root)
            external_target = root / "external-target"

            skill_file = skill_directory / "SKILL.md"
            skill_file.unlink()
            skill_file.symlink_to(external_target)
            with self.assertRaisesRegex(onboarding.OnboardingError, "symbolic link"):
                onboarding.ensure_installation(
                    config_file=config_file,
                    home=root / "home",
                    input_stream=io.StringIO(),
                    output_stream=io.StringIO(),
                    canonical_skill_file=self.canonical_skill,
                )

            skill_file.unlink()
            skill_file.write_bytes(self.canonical_skill.read_bytes())
            profile_file = skill_directory / "PROFILE.md"
            profile_file.unlink()
            profile_file.symlink_to(external_target)
            with self.assertRaisesRegex(onboarding.OnboardingError, "symbolic link"):
                onboarding.ensure_installation(
                    config_file=config_file,
                    home=root / "home",
                    input_stream=io.StringIO(),
                    output_stream=io.StringIO(),
                    canonical_skill_file=self.canonical_skill,
                )

    def test_setup_preserves_or_replaces_an_existing_profile_after_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skill_root = root / "skills"
            config_file = root / "config.json"
            skill_directory = self.write_complete_installation(skill_root)
            self.write_config(config_file, skill_root)
            profile_file = skill_directory / "PROFILE.md"
            original_profile = profile_file.read_text(encoding="utf-8")

            cli.main(
                ["setup"],
                config_file=config_file,
                home=root / "home",
                input_stream=InteractiveInput("science\n2\n" + str(skill_root) + "\nwar\nn\n"),
                onboarding_output=io.StringIO(),
                canonical_skill_file=self.canonical_skill,
            )
            self.assertEqual(profile_file.read_text(encoding="utf-8"), original_profile)

            cli.main(
                ["setup"],
                config_file=config_file,
                home=root / "home",
                input_stream=InteractiveInput("science\n2\n" + str(skill_root) + "\nwar\ny\n"),
                onboarding_output=io.StringIO(),
                canonical_skill_file=self.canonical_skill,
            )
            profile = profile_file.read_text(encoding="utf-8")
            self.assertIn("science", profile)
            self.assertIn("war", profile)

    def test_unconfigured_noninteractive_fetch_has_no_stdout_and_skips_loading(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_file = Path(temporary_directory) / "config.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(cli, "load_candidates") as load, redirect_stdout(
                stdout
            ), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as error:
                    cli.main(
                        ["fetch"],
                        config_file=config_file,
                        input_stream=io.StringIO(),
                    )

        self.assertEqual(error.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("interactive terminal", stderr.getvalue())
        load.assert_not_called()

    def test_configured_preflight_keeps_fetch_stdout_machine_readable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            skill_root = root / "skills"
            config_file = root / "config.json"
            self.write_complete_installation(skill_root)
            self.write_config(config_file, skill_root)
            stdout = io.StringIO()
            stderr = io.StringIO()
            candidates = [
                make_candidate("One"),
                make_candidate("Two"),
                make_candidate("Three"),
                make_candidate("Extra"),
            ]

            with patch.object(cli, "load_candidates", return_value=candidates), redirect_stdout(
                stdout
            ), redirect_stderr(stderr):
                cli.main(
                    ["fetch"],
                    config_file=config_file,
                    home=root / "home",
                    input_stream=io.StringIO(),
                    history_file=root / "recommendation-history.json",
                )

        self.assertEqual(json.loads(stdout.getvalue())["status"], "ready")
        self.assertEqual(stderr.getvalue(), "")

    def test_help_runs_preflight_before_argparse_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_file = root / "config.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as error:
                    cli.main(
                        ["--help"],
                        config_file=config_file,
                        home=root / "home",
                        input_stream=io.StringIO(),
                    )
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("interactive terminal", stderr.getvalue())

            skill_root = root / "skills"
            self.write_complete_installation(skill_root)
            self.write_config(config_file, skill_root)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as error:
                    cli.main(
                        ["--help"],
                        config_file=config_file,
                        home=root / "home",
                        input_stream=io.StringIO(),
                    )

        self.assertEqual(error.exception.code, 0)
        self.assertIn("usage:", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_malformed_config_and_non_directory_root_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_file = root / "config.json"
            config_file.write_text("{invalid", encoding="utf-8")
            with self.assertRaisesRegex(onboarding.OnboardingError, "Could not read"):
                onboarding.ensure_installation(
                    config_file=config_file,
                    home=root / "home",
                    input_stream=io.StringIO(),
                    output_stream=io.StringIO(),
                )

            skill_root = root / "not-a-directory"
            skill_root.write_text("file", encoding="utf-8")
            self.write_config(config_file, skill_root)
            with self.assertRaisesRegex(onboarding.OnboardingError, "not a directory"):
                onboarding.ensure_installation(
                    config_file=config_file,
                    home=root / "home",
                    input_stream=io.StringIO(),
                    output_stream=io.StringIO(),
                )

    def test_canonical_skill_resolves_to_the_root_original_in_a_checkout(self):
        self.assertEqual(onboarding.canonical_skill_path(), self.canonical_skill)
        self.assertEqual(
            onboarding.canonical_skill_path().read_bytes(),
            self.canonical_skill.read_bytes(),
        )

    def test_default_paths_use_the_dokutipp_home_and_hermes_home_fallback(self):
        home = Path("/tmp/dokutipp-home")
        self.assertEqual(
            onboarding.config_path(
                environment={"XDG_CONFIG_HOME": "/tmp/dokutipp-xdg"},
                home=home,
            ),
            home / ".dokutipp/config.json",
        )
        self.assertEqual(
            onboarding.config_path(environment={}, home=home),
            home / ".dokutipp/config.json",
        )
        self.assertEqual(
            cli.default_data_dir(home),
            home / ".dokutipp/data",
        )
        self.assertEqual(
            onboarding.hermes_skill_root(environment={}, home=home),
            home / ".hermes/skills",
        )


def make_candidate(
    title,
    *,
    channel="ARD",
    date="11.08.2026",
    duration="00:42:00",
    description="A source description.",
    website="https://example.invalid/documentary",
):
    return {
        "title": title,
        "channel": channel,
        "date": date,
        "duration": duration,
        "description": description,
        "website": website,
    }


def make_entry(
    sender,
    title,
    duration,
    timestamp,
    *,
    description="A test description.",
    website="https://example.invalid/documentary",
):
    entry = [""] * 17
    entry[0] = sender
    entry[1] = "Documentaries"
    entry[2] = title
    entry[3] = "11.08.2026"
    entry[5] = duration
    entry[7] = description
    entry[9] = website
    entry[16] = str(timestamp)
    return entry


def write_filmliste(path, entries):
    raw_data = "{\n" + ",\n".join(
        '"X": ' + json.dumps(entry) for entry in entries
    ) + "\n}\n"
    with lzma.open(path, "wt", encoding="utf-8") as file_handle:
        file_handle.write(raw_data)
