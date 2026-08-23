import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from dokutipp import cli, selection


def make_candidate(number):
    return {
        "title": f"Candidate {number:05d}",
        "channel": f"Channel {number % 7}",
        "date": "23.08.2026",
        "duration": "00:50:00",
        "description": f"Description {number}",
        "website": f"https://example.invalid/{number}",
    }


class PaginationTests(unittest.TestCase):
    def test_pages_are_deterministic_and_report_one_based_ranges(self):
        candidates = [make_candidate(number) for number in range(123)]

        first = selection.paginate_candidates(candidates, limit=50, page=1)
        middle = selection.paginate_candidates(
            list(reversed(candidates)), limit=50, page=2
        )
        last = selection.paginate_candidates(candidates, limit=50, page=3)

        self.assertEqual((first.total_pages, first.start, first.end), (3, 1, 50))
        self.assertEqual((middle.total_pages, middle.start, middle.end), (3, 51, 100))
        self.assertEqual((last.total_pages, last.start, last.end), (3, 101, 123))
        self.assertEqual(len(first.candidates), 50)
        self.assertEqual(len(middle.candidates), 50)
        self.assertEqual(len(last.candidates), 23)
        self.assertEqual(
            [selection.candidate_id(candidate) for candidate in middle.candidates],
            [
                selection.candidate_id(candidate)
                for candidate in selection.paginate_candidates(
                    candidates, limit=50, page=2
                ).candidates
            ],
        )

    def test_history_exclusion_and_deduplication_happen_before_page_slicing(self):
        first = make_candidate(1)
        repeated = dict(first, description="Updated description")
        second = make_candidate(2)
        pool = selection.build_candidate_pool(
            [first, repeated, second],
            excluded_ids={selection.candidate_id(second)},
        )

        page = selection.paginate_candidates(pool, limit=50, page=1)

        self.assertEqual(page.total_candidates, 1)
        self.assertEqual(page.candidates, (first,))

    def test_fetch_payload_contains_page_metadata_and_only_page_candidates(self):
        candidates = [make_candidate(number) for number in range(9)]
        payload = selection.build_fetch_payload(
            candidates,
            limit=4,
            page=2,
            min_duration=42,
            excluded_channels=(),
        )

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(
            payload["pagination"],
            {
                "page": 2,
                "total_pages": 3,
                "limit": 4,
                "total_candidates": 9,
                "candidate_range": {"start": 5, "end": 8},
            },
        )
        self.assertEqual(len(payload["candidates"]), 4)
        self.assertEqual(payload["pagination"]["page"], 2)
        self.assertNotIn("website", json.dumps(payload))

    def test_last_short_page_remains_usable_for_cross_page_selection(self):
        payload = selection.build_fetch_payload(
            [make_candidate(number) for number in range(5)],
            limit=4,
            page=2,
            min_duration=42,
            excluded_channels=(),
        )

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(len(payload["candidates"]), 1)
        self.assertNotIn("message", payload)

    def test_invalid_page_is_structured_by_the_cli(self):
        with self.assertRaises(selection.PaginationError) as context:
            selection.paginate_candidates(
                [make_candidate(number) for number in range(4)],
                limit=4,
                page=2,
            )

        error = context.exception
        self.assertEqual(
            {
                "page": error.page,
                "total_pages": error.total_pages,
                "limit": error.limit,
                "total_candidates": error.total_candidates,
            },
            {"page": 2, "total_pages": 1, "limit": 4, "total_candidates": 4},
        )

    def test_out_of_range_page_writes_structured_error_without_result_or_history(self):
        candidates = [make_candidate(number) for number in range(4)]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            data_dir = root / "data"
            history_file = root / "history.json"
            history_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "selected_at": {
                            selection.candidate_id(candidates[0]): 1_800_000_000.0
                        },
                    }
                ),
                encoding="utf-8",
            )
            history_before = history_file.read_bytes()
            stdout = io.StringIO()
            stderr = io.StringIO()

            with patch.object(cli, "ensure_installation", return_value=False), patch.object(
                cli,
                "load_config",
                return_value=SimpleNamespace(sender_filter_file=None),
            ), patch.object(cli, "load_sender_filters", return_value=()), patch.object(
                cli, "load_candidates", return_value=candidates
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as context:
                    cli.main(
                        ["fetch", "--limit", "4", "--page", "2"],
                        data_dir=data_dir,
                        history_file=history_file,
                        history_now=1_800_000_000.0,
                    )

            self.assertEqual(context.exception.code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(history_file.read_bytes(), history_before)
            self.assertEqual(
                json.loads(stderr.getvalue()),
                {
                    "type": "error",
                    "error_code": "page_out_of_range",
                    "page": 2,
                    "total_pages": 1,
                    "limit": 4,
                    "total_candidates": 3,
                    "message": "Page 2 is outside the available range 1..1.",
                },
            )

    def test_selection_can_combine_candidates_from_browsed_pages(self):
        candidates = [make_candidate(number) for number in range(8)]
        with tempfile.TemporaryDirectory() as temporary_directory:
            history_file = Path(temporary_directory) / "history.json"
            with patch.object(cli, "load_candidates", return_value=candidates):
                first_page = cli.run_fetch(
                    limit=4,
                    page=1,
                    history_file=history_file,
                    output=io.StringIO(),
                )
                second_page = cli.run_fetch(
                    limit=4,
                    page=2,
                    history_file=history_file,
                    output=io.StringIO(),
                )
            first_ids = [candidate["id"] for candidate in first_page["candidates"]]
            second_ids = [candidate["id"] for candidate in second_page["candidates"]]
            argument = ",".join(
                [first_ids[0], second_ids[0], first_ids[1], f"x{second_ids[1]}"]
            )

            with patch.object(cli, "load_candidates", return_value=candidates):
                cli.run_select(
                    argument,
                    limit=4,
                    page=2,
                    history_file=history_file,
                    output=io.StringIO(),
                )


if __name__ == "__main__":
    unittest.main()
