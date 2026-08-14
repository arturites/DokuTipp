import io
import json
import lzma
import os
import sys
import tempfile
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

from dokutipp import cli, parser, selection
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

    def test_registry_rejects_candidates_with_an_ambiguous_hash_collision(self):
        first = make_candidate("Collision documentary", description="First synopsis.")
        second = dict(first, description="Different synopsis, same identity fields.")

        self.assertEqual(candidate_id(first), candidate_id(second))
        with self.assertRaisesRegex(SelectionError, "Ambiguous candidate ID"):
            build_candidate_registry([first, second])


class FetchPayloadTests(unittest.TestCase):
    def setUp(self):
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
                channels=("ZDF", "ARTE.DE"),
                filter_file=None,
                output=output,
                today=date(2026, 8, 12),
            )

        load.assert_called_once_with(
            data_dir=data_dir,
            limit=17,
            min_duration=55,
            channels=("ZDF", "ARTE.DE"),
            filter_file=None,
        )
        self.assertEqual(payload["status"], "ready")
        self.assertNotIn("message", payload)
        self.assertEqual(
            payload["selection"],
            {
                "normal_recommendations": 3,
                "extra_recommendations": 1,
                "extra_id_prefix": "x",
                "argument_format": "ID1,ID2,ID3,xID4",
            },
        )
        self.assertEqual(
            payload["filters"],
            {
                "limit": 17,
                "min_duration": 55,
                "channels": ["ZDF", "ARTE.DE"],
                "title_exclusions": list(parser.load_title_filters()),
            },
        )
        self.assertEqual(
            [candidate["id"] for candidate in payload["candidates"]],
            [candidate_id(candidate) for candidate in self.candidates],
        )
        self.assertEqual(json.loads(output.getvalue()), payload)
        self.assertNotIn("website", payload["candidates"][0])
        self.assertNotIn(self.candidates[0]["website"], output.getvalue())

    def test_cli_limit_defaults_to_no_limit(self):
        arguments = cli.build_argument_parser().parse_args(["fetch"])

        self.assertIsNone(arguments.limit)

        output = io.StringIO()
        with patch.object(cli, "load_candidates", return_value=self.candidates) as load:
            cli.run_fetch(output=output, today=date(2026, 8, 12))

        self.assertIsNone(load.call_args.kwargs["limit"])
        self.assertIsNone(json.loads(output.getvalue())["filters"]["limit"])

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
                        output=output,
                        today=date(2026, 8, 12),
                    )

                self.assertEqual(payload["status"], expected_status)
                self.assertIn(expected_message, payload["message"])
                self.assertEqual(json.loads(output.getvalue()), payload)
                self.assertNotIn("website", output.getvalue())
                for candidate in candidates:
                    self.assertNotIn(candidate["website"], output.getvalue())


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
            filmliste = Path(temporary_directory) / cli.FILMLISTE_FILENAME
            self.assertTrue(cli.needs_download(filmliste))
            filmliste.touch()
            modified_at = filmliste.stat().st_mtime

            with patch.object(cli.time, "time", return_value=modified_at + 1):
                self.assertFalse(cli.needs_download(filmliste))
            with patch.object(
                cli.time,
                "time",
                return_value=modified_at + cli.MAX_AGE_SECONDS + 1,
            ):
                self.assertTrue(cli.needs_download(filmliste))

    def test_ensure_filmliste_uses_fresh_cache_without_curl(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory) / "data"
            stderr = io.StringIO()
            with patch.object(cli, "needs_download", return_value=False):
                with patch.object(cli.subprocess, "run") as curl:
                    with redirect_stderr(stderr):
                        filmliste = cli.ensure_filmliste(data_dir)

        self.assertEqual(filmliste, data_dir / cli.FILMLISTE_FILENAME)
        curl.assert_not_called()
        self.assertIn("Filmliste-akt.xz is fresh", stderr.getvalue())

    def test_ensure_filmliste_downloads_missing_or_stale_cache(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory) / "data"
            expected_filmliste = data_dir / cli.FILMLISTE_FILENAME
            stderr = io.StringIO()
            with patch.object(cli, "needs_download", return_value=True):
                with patch.object(
                    cli.subprocess, "run", return_value=Mock(returncode=0)
                ) as curl:
                    with redirect_stderr(stderr):
                        filmliste = cli.ensure_filmliste(data_dir)

        self.assertEqual(filmliste, expected_filmliste)
        curl.assert_called_once_with(
            ["curl", "-fsSL", "-o", str(expected_filmliste), cli.DOWNLOAD_URL]
        )
        self.assertIn("Downloading Filmliste-akt.xz", stderr.getvalue())
        self.assertIn("Download complete", stderr.getvalue())

    def test_ensure_filmliste_reports_a_failed_download(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory) / "data"
            stderr = io.StringIO()
            with patch.object(cli, "needs_download", return_value=True):
                with patch.object(
                    cli.subprocess, "run", return_value=Mock(returncode=1)
                ):
                    with redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as error:
                            cli.ensure_filmliste(data_dir)

        self.assertEqual(error.exception.code, 1)
        self.assertIn("Download of Filmliste-akt.xz failed", stderr.getvalue())


class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [
            make_candidate("One", website="https://example.invalid/one"),
            make_candidate("Two", website="https://example.invalid/two"),
            make_candidate("Three", website="https://example.invalid/three"),
            make_candidate("Extra", website="https://example.invalid/extra"),
        ]

    def test_bare_cli_prints_help_to_stderr_and_exits_with_status_two(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as error:
                cli.main([])

        self.assertEqual(error.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("usage:", stderr.getvalue())
        self.assertIn("fetch", stderr.getvalue())
        self.assertIn("select", stderr.getvalue())

    def test_load_candidates_reuses_the_existing_parser_filters(self):
        filmliste = Path("/tmp/Filmliste-akt.xz")
        filter_file = Path("/tmp/filters.txt")
        with patch.object(cli, "ensure_filmliste", return_value=filmliste):
            with patch.object(cli, "parse_filmliste", return_value=self.candidates) as parse:
                result = cli.load_candidates(
                    data_dir=Path("/tmp/data"),
                    limit=12,
                    min_duration=90,
                    channels=("ZDF", "ARTE.DE"),
                    filter_file=filter_file,
                )

        self.assertIs(result, self.candidates)
        parse.assert_called_once_with(
            filmliste,
            limit=12,
            min_duration=90,
            channels=("ZDF", "ARTE.DE"),
            filter_file=filter_file,
        )

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
            with redirect_stdout(fetch_stdout), redirect_stderr(fetch_stderr):
                cli.main(
                    [
                        "fetch",
                        "--limit",
                        "4",
                        "--min-duration",
                        "42",
                        "--channels",
                        "ARD",
                        "ZDF",
                        "ARTE.DE",
                        "--filter-file",
                        str(filter_file),
                    ],
                    data_dir=data_dir,
                )

            fetch_payload = json.loads(fetch_stdout.getvalue())
            candidate_ids = [candidate["id"] for candidate in fetch_payload["candidates"]]
            selection_argument = ",".join(
                [
                    candidate_ids[2],
                    f"{EXTRA_ID_PREFIX}{candidate_ids[3]}",
                    candidate_ids[0],
                    candidate_ids[1],
                ]
            )

            select_stdout = io.StringIO()
            select_stderr = io.StringIO()
            with redirect_stdout(select_stdout), redirect_stderr(select_stderr):
                cli.main(
                    [
                        "select",
                        selection_argument,
                        "--limit",
                        "4",
                        "--min-duration",
                        "42",
                        "--channels",
                        "ARD",
                        "ZDF",
                        "ARTE.DE",
                        "--filter-file",
                        str(filter_file),
                    ],
                    data_dir=data_dir,
                )

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

    def test_select_validation_failure_writes_only_a_stderr_error(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(cli, "load_candidates", return_value=self.candidates):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as error:
                    cli.main(["select", "not-a-valid-selection"])

        self.assertEqual(error.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Error: Selection must contain exactly four", stderr.getvalue())

    def test_invalid_filter_file_is_reported_as_cli_error(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            filter_file = Path(temporary_directory) / "filters.txt"
            filter_file.write_text("[unterminated", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()

            with patch.object(cli, "load_candidates", return_value=[]):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as error:
                        cli.main(
                            ["fetch", "--filter-file", str(filter_file)]
                        )

        self.assertEqual(error.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Invalid title filter", stderr.getvalue())

    def test_explicit_filter_file_is_used_for_fetch_metadata(self):
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
                    output=output,
                )

        self.assertEqual(payload["filters"]["title_exclusions"], ["Mittags[- ]magazin"])

    def test_select_workflow_does_not_read_skill_md(self):
        identifiers = [candidate_id(candidate) for candidate in self.candidates]
        selection_argument = (
            f"{identifiers[0]},{identifiers[1]},{identifiers[2]},"
            f"x{identifiers[3]}"
        )
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as temporary_directory:
            original_cwd = Path.cwd()
            try:
                os.chdir(temporary_directory)
                with patch.object(cli, "load_candidates", return_value=self.candidates):
                    with patch.object(
                        Path,
                        "read_text",
                        side_effect=AssertionError("SKILL.md must not be read"),
                    ):
                        cli.run_select(
                            selection_argument,
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
                channels=parser.DEFAULT_CHANNELS,
            )

        self.assertEqual(
            [entry["title"] for entry in results],
            ["Eligible documentary", "Wrong channel"],
        )

    def test_parser_filters_senders_when_explicitly_requested(self):
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
                channels=("ARD",),
            )

        self.assertEqual([entry["title"] for entry in results], ["ARD documentary"])

    def test_default_title_filters_exclude_formats_but_preserve_documentaries(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            filmliste = Path(temporary_directory) / cli.FILMLISTE_FILENAME
            now = int(time.time())
            entries = [
                make_entry(
                    "ZDF",
                    "Volle Kanne vom 14. August 2026",
                    "00:50:00",
                    now,
                ),
                make_entry("ARTE.DE", "Wakefield (1/8)", "00:50:00", now),
                make_entry(
                    "ARD",
                    "Leichtathletik-EM: Frühsession vom 13. August",
                    "00:50:00",
                    now,
                ),
                make_entry("ZDF", "Trailer: Neue Serie", "00:50:00", now),
                make_entry(
                    "ARTE.DE",
                    "ARTE Reportage - Russland / Madagaskar",
                    "00:50:00",
                    now,
                ),
                make_entry(
                    "ARTE.DE",
                    "Stadt Land Kunst - Japan / El Salvador / Delphi",
                    "00:50:00",
                    now,
                ),
                make_entry(
                    "ARTE.DE",
                    "Studio 54 - Hinter den Pforten des legendären Clubs",
                    "00:50:00",
                    now,
                ),
            ]
            write_filmliste(filmliste, entries)

            results = parser.parse_filmliste(
                filmliste,
                min_duration=cli.DEFAULT_MIN_DURATION,
                channels=parser.DEFAULT_CHANNELS,
            )

        self.assertEqual(
            [entry["title"] for entry in results],
            [
                "ARTE Reportage - Russland / Madagaskar",
                "Stadt Land Kunst - Japan / El Salvador / Delphi",
                "Studio 54 - Hinter den Pforten des legendären Clubs",
            ],
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
                channels=parser.DEFAULT_CHANNELS,
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
