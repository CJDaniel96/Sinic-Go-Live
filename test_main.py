import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

from main import (
    is_timestamp_folder_for_date,
    list_candidate_folders,
    load_config,
    parse_args,
    start_event_observer,
)


class TimestampFolderTests(unittest.TestCase):
    def test_accepts_valid_timestamp_from_target_date(self) -> None:
        target_date = date(2026, 8, 5)

        self.assertTrue(
            is_timestamp_folder_for_date(Path("20260805123456"), target_date)
        )

    def test_rejects_other_dates_and_invalid_timestamps(self) -> None:
        target_date = date(2026, 8, 5)

        self.assertFalse(
            is_timestamp_folder_for_date(Path("20260804123456"), target_date)
        )
        self.assertFalse(
            is_timestamp_folder_for_date(Path("20260805256000"), target_date)
        )
        self.assertFalse(is_timestamp_folder_for_date(Path("incoming"), target_date))

    def test_lists_only_timestamp_folders_from_target_date(self) -> None:
        target_date = date(2026, 8, 5)

        with tempfile.TemporaryDirectory() as temp_dir:
            input_dir = Path(temp_dir)
            today_early = input_dir / "20260805000102"
            today_late = input_dir / "20260805235959"
            yesterday = input_dir / "20260804235959"
            invalid = input_dir / "archive"
            for folder in (today_early, today_late, yesterday, invalid):
                folder.mkdir()

            self.assertEqual(
                list_candidate_folders(input_dir, target_date),
                (today_early, today_late),
            )

    def test_supports_a_timestamp_folder_as_the_input_directory(self) -> None:
        target_date = date(2026, 8, 5)

        with tempfile.TemporaryDirectory() as temp_dir:
            input_dir = Path(temp_dir) / "20260805112233"
            input_dir.mkdir()
            (input_dir / "result.csv").write_text("id\n1\n", encoding="utf-8")

            self.assertEqual(
                list_candidate_folders(input_dir, target_date),
                (input_dir,),
            )


class EventWatchTests(unittest.TestCase):
    def test_loads_event_watch_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            input_dir = base_dir / "input"
            return_dir = base_dir / "return"
            input_dir.mkdir()

            args = parse_args(
                [
                    "--input-dir",
                    str(input_dir),
                    "--return-dir",
                    str(return_dir),
                    "--watch-mode",
                    "event",
                    "--event-rescan-seconds",
                    "45",
                ]
            )
            config = load_config(args)

            self.assertEqual(config.watch.mode, "event")
            self.assertEqual(config.watch.event_rescan_seconds, 45.0)

    def test_event_observer_detects_a_new_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_dir = Path(temp_dir)
            change_event = threading.Event()
            observer = start_event_observer(input_dir, change_event)

            try:
                (input_dir / "20260805123456").mkdir()
                self.assertTrue(
                    change_event.wait(5.0),
                    "The event observer did not detect the new folder",
                )
            finally:
                observer.stop()
                observer.join()


if __name__ == "__main__":
    unittest.main()
