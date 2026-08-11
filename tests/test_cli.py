import json
import lzma
import os
import runpy
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from dokutipp import cli, parser


class CliDefaultsTests(unittest.TestCase):
    def capture_workflow(self, invoke):
        commands = []

        def fake_subprocess_run(command, **kwargs):
            commands.append((command, kwargs))
            return Mock(returncode=0)

        with patch.object(cli, "needs_download", return_value=False):
            with patch.object(cli, "log"):
                with patch.object(
                    cli.subprocess, "run", side_effect=fake_subprocess_run
                ):
                    invoke()

        return commands

    def test_bare_cli_matches_legacy_default_workflow(self):
        legacy_data_dir = REPOSITORY_ROOT / "data"
        legacy_workflow = self.capture_workflow(
            lambda: cli.run_default(data_dir=legacy_data_dir)
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            original_cwd = Path.cwd()
            try:
                os.chdir(temporary_directory)
                cli_workflow = self.capture_workflow(lambda: cli.main([]))
            finally:
                os.chdir(original_cwd)

        expected_command = [
            sys.executable,
            str(Path(cli.__file__).with_name("parser.py")),
            str(legacy_data_dir / cli.FILMLISTE_FILENAME),
            "--limit",
            "1337",
            "--min-duration",
            "42",
        ]
        self.assertEqual(legacy_workflow, cli_workflow)
        self.assertEqual(cli_workflow, [(expected_command, {"check": True})])

    def test_existing_filter_options_override_defaults(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory) / "data"
            workflow = self.capture_workflow(
                lambda: cli.main(
                    [
                        "--limit",
                        "12",
                        "--min-duration",
                        "90",
                        "--channels",
                        "ZDF",
                        "ARTE.DE",
                    ],
                    data_dir=data_dir,
                )
            )

        self.assertEqual(
            workflow,
            [
                (
                    [
                        sys.executable,
                        str(Path(cli.__file__).with_name("parser.py")),
                        str(data_dir / cli.FILMLISTE_FILENAME),
                        "--limit",
                        "12",
                        "--min-duration",
                        "90",
                        "--channels",
                        "ZDF",
                        "ARTE.DE",
                    ],
                    {"check": True},
                )
            ],
        )

    def test_parser_preserves_default_filters_with_fresh_cache(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            filmliste = Path(temporary_directory) / cli.FILMLISTE_FILENAME
            now = int(time.time())

            entries = [
                self.make_entry("ARD", "Eligible documentary", "00:42:00", now),
                self.make_entry("", "Too short", "00:41:00", now),
                self.make_entry("ZDF", "Audiodeskription version", "00:50:00", now),
                self.make_entry("ARTE.DE", "Too old", "00:50:00", now - 8 * 24 * 3600),
            ]
            raw_data = "{\n" + ",\n".join(
                '"X": ' + json.dumps(entry) for entry in entries
            ) + "\n}\n"
            with lzma.open(filmliste, "wt", encoding="utf-8") as file_handle:
                file_handle.write(raw_data)

            results = parser.parse_filmliste(
                filmliste,
                limit=cli.DEFAULT_LIMIT,
                min_duration=cli.DEFAULT_MIN_DURATION,
                channels=parser.DEFAULT_CHANNELS,
            )

        self.assertEqual([entry["title"] for entry in results], ["Eligible documentary"])

    def test_captured_stdout_preserves_legacy_subprocess_order(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            data_dir = temporary_path / "data"
            data_dir.mkdir()
            filmliste = data_dir / cli.FILMLISTE_FILENAME
            entry = self.make_entry(
                "ARD", "Subprocess order fixture", "00:42:00", int(time.time())
            )
            with lzma.open(filmliste, "wt", encoding="utf-8") as file_handle:
                file_handle.write('{"X": ' + json.dumps(entry) + "}")

            environment = os.environ.copy()
            existing_python_path = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = str(SOURCE_ROOT)
            if existing_python_path:
                environment["PYTHONPATH"] += os.pathsep + existing_python_path
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; "
                        "from dokutipp.cli import run_default; "
                        "run_default(data_dir=Path('data'))"
                    ),
                ],
                cwd=temporary_path,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertLess(
            result.stdout.index('"title"'),
            result.stdout.index("Filmliste-akt.xz is fresh"),
        )

    def test_legacy_start_script_delegates_to_shared_default(self):
        with patch.object(cli, "run_default") as run_default:
            runpy.run_path(
                str(REPOSITORY_ROOT / "scripts" / "start_curation.py"),
                run_name="__main__",
            )

        run_default.assert_called_once_with(data_dir=REPOSITORY_ROOT / "data")

    def test_legacy_parser_script_reexports_parser_helpers(self):
        namespace = runpy.run_path(
            str(REPOSITORY_ROOT / "scripts" / "parse_filmliste.py")
        )
        self.assertIs(namespace["parse_raw"], parser.parse_raw)
        self.assertIs(namespace["parse_filmliste"], parser.parse_filmliste)

    @staticmethod
    def make_entry(sender, title, duration, timestamp):
        entry = [""] * 17
        entry[0] = sender
        entry[1] = "Documentaries"
        entry[2] = title
        entry[3] = "11.08.2026"
        entry[5] = duration
        entry[7] = "A test description."
        entry[9] = "https://example.invalid/documentary"
        entry[16] = str(timestamp)
        return entry
