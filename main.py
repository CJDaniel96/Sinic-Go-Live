from __future__ import annotations

import argparse
import csv
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_RESULT_CODE = "23"
CSV_SUFFIX = ".csv"
IMAGE_SUFFIXES = {".jpg", ".jpeg"}


@dataclass(frozen=True)
class MachineJob:
    folder: Path
    csv_files: tuple[Path, ...]
    image_files: tuple[Path, ...]


@dataclass
class FolderState:
    snapshot: tuple[tuple[str, int, int], ...]
    stable_since: float
    first_seen: float
    last_status: str = ""
    last_timeout_warning: float = 0.0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Watch machine output folders, set the CSV is_pass column to a "
            "fixed AI result code, and return the CSV to the machine folder."
        )
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        type=Path,
        help="Machine output root directory. Timestamp subfolders are scanned.",
    )
    parser.add_argument(
        "--return-dir",
        required=True,
        type=Path,
        help="Directory where processed CSV files are returned.",
    )
    parser.add_argument(
        "--result-code",
        default=DEFAULT_RESULT_CODE,
        help="Value written to every is_pass cell. Default: 23 (AING).",
    )
    parser.add_argument(
        "--poll-interval",
        default=1.0,
        type=float,
        help="Seconds between directory scans. Default: 1.0.",
    )
    parser.add_argument(
        "--settle-seconds",
        default=2.0,
        type=float,
        help="Process only after CSV/JPG file sizes stop changing. Default: 2.0.",
    )
    parser.add_argument(
        "--ready-timeout",
        default=300.0,
        type=float,
        help=(
            "Seconds before warning/failing on an incomplete folder. "
            "Use 0 to disable. Default: 300."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process currently available folders and exit.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite returned CSV files if they already exist.",
    )
    parser.add_argument(
        "--allow-no-images",
        action="store_true",
        help="Do not wait for JPG/JPEG files before processing a CSV.",
    )
    parser.add_argument(
        "--preserve-folder",
        action="store_true",
        help="Return CSVs under return-dir/<timestamp-folder>/ instead of return-dir/.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Logging verbosity. Default: INFO.",
    )
    return parser.parse_args(argv)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def list_candidate_folders(input_dir: Path) -> tuple[Path, ...]:
    folders: list[Path] = []
    if find_csv_files(input_dir):
        folders.append(input_dir)

    for child in input_dir.iterdir():
        if child.is_dir():
            folders.append(child)

    return tuple(sorted(folders, key=lambda item: item.name))


def scan_job(folder: Path) -> MachineJob:
    return MachineJob(
        folder=folder,
        csv_files=find_csv_files(folder),
        image_files=find_image_files(folder),
    )


def find_csv_files(folder: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() == CSV_SUFFIX
        )
    )


def find_image_files(folder: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        )
    )


def snapshot_job(job: MachineJob) -> tuple[tuple[str, int, int], ...]:
    snapshot: list[tuple[str, int, int]] = []
    for path in (*job.csv_files, *job.image_files):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        snapshot.append((path.name, stat.st_size, stat.st_mtime_ns))
    return tuple(sorted(snapshot))


def status_for_job(job: MachineJob, allow_no_images: bool) -> str:
    if not job.csv_files:
        return "waiting for CSV"
    if not allow_no_images and not job.image_files:
        return "waiting for JPG images"
    return (
        f"waiting for files to settle "
        f"({len(job.csv_files)} CSV, {len(job.image_files)} image)"
    )


def is_ready(job: MachineJob, state: FolderState, args: argparse.Namespace) -> bool:
    if not job.csv_files:
        return False
    if not args.allow_no_images and not job.image_files:
        return False
    return (time.monotonic() - state.stable_since) >= args.settle_seconds


def return_csv_path(source_folder: Path, csv_path: Path, args: argparse.Namespace) -> Path:
    if args.preserve_folder:
        return args.return_dir / source_folder.name / csv_path.name
    return args.return_dir / csv_path.name


def outputs_already_exist(job: MachineJob, args: argparse.Namespace) -> bool:
    if args.overwrite or not job.csv_files:
        return False
    return all(return_csv_path(job.folder, csv_path, args).exists() for csv_path in job.csv_files)


def detect_encoding(path: Path) -> str:
    sample = path.read_bytes()[:4096]
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"

    for encoding in ("utf-8", "cp950", "big5"):
        try:
            sample.decode(encoding)
        except UnicodeDecodeError:
            continue
        return encoding

    return "latin1"


def detect_lineterminator(path: Path) -> str:
    sample = path.read_bytes()[:4096]
    newline_index = sample.find(b"\n")
    if newline_index <= 0:
        return "\n"
    if sample[newline_index - 1 : newline_index] == b"\r":
        return "\r\n"
    return "\n"


def detect_dialect(csv_path: Path, encoding: str) -> type[csv.Dialect] | csv.Dialect:
    with csv_path.open("r", encoding=encoding, newline="") as file:
        sample = file.read(4096)
    try:
        return csv.Sniffer().sniff(sample)
    except csv.Error:
        return csv.excel


def update_csv_is_pass(source_csv: Path, destination_csv: Path, result_code: str) -> int:
    encoding = detect_encoding(source_csv)
    lineterminator = detect_lineterminator(source_csv)
    dialect = detect_dialect(source_csv, encoding)
    destination_csv.parent.mkdir(parents=True, exist_ok=True)
    temp_csv = destination_csv.with_name(
        f".{destination_csv.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    )

    row_count = 0
    try:
        with source_csv.open("r", encoding=encoding, newline="") as source_file:
            reader = csv.DictReader(source_file, dialect=dialect)
            fieldnames = reader.fieldnames
            if not fieldnames:
                raise ValueError(f"{source_csv} has no CSV header")
            if "is_pass" not in fieldnames:
                raise ValueError(f"{source_csv} does not contain required column: is_pass")

            with temp_csv.open("w", encoding=encoding, newline="") as destination_file:
                writer = csv.DictWriter(
                    destination_file,
                    fieldnames=fieldnames,
                    dialect=dialect,
                    lineterminator=lineterminator,
                    extrasaction="ignore",
                )
                writer.writeheader()
                for row in reader:
                    row["is_pass"] = result_code
                    writer.writerow(row)
                    row_count += 1
                destination_file.flush()
                os.fsync(destination_file.fileno())

        os.replace(temp_csv, destination_csv)
        return row_count
    except Exception:
        temp_csv.unlink(missing_ok=True)
        raise


def process_job(job: MachineJob, args: argparse.Namespace) -> None:
    logging.info(
        "Processing %s (%d CSV, %d image)",
        job.folder,
        len(job.csv_files),
        len(job.image_files),
    )
    for csv_path in job.csv_files:
        destination_csv = return_csv_path(job.folder, csv_path, args)
        row_count = update_csv_is_pass(csv_path, destination_csv, args.result_code)
        logging.info("Returned %s (%d rows)", destination_csv, row_count)


def folder_key(folder: Path) -> str:
    return str(folder.resolve())


def run(args: argparse.Namespace) -> int:
    if not args.input_dir.exists() or not args.input_dir.is_dir():
        logging.error("Input directory does not exist or is not a directory: %s", args.input_dir)
        return 2

    args.return_dir.mkdir(parents=True, exist_ok=True)

    logging.info("Input directory: %s", args.input_dir)
    logging.info("Return directory: %s", args.return_dir)
    logging.info("Mode: %s", "once" if args.once else "watch")
    logging.info("Writing is_pass=%s", args.result_code)

    states: dict[str, FolderState] = {}
    processed: set[str] = set()

    while True:
        now = time.monotonic()
        candidate_folders = list_candidate_folders(args.input_dir)
        if not candidate_folders and args.once:
            logging.warning("No timestamp folders or CSV files found in %s", args.input_dir)
            return 0

        for folder in candidate_folders:
            key = folder_key(folder)
            if key in processed:
                continue

            job = scan_job(folder)
            if outputs_already_exist(job, args):
                logging.info("Skipping %s because returned CSV already exists", folder)
                processed.add(key)
                states.pop(key, None)
                continue

            snapshot = snapshot_job(job)
            state = states.get(key)
            if state is None:
                state = FolderState(snapshot=snapshot, stable_since=now, first_seen=now)
                states[key] = state
            elif snapshot != state.snapshot:
                state.snapshot = snapshot
                state.stable_since = now

            status = status_for_job(job, args.allow_no_images)
            if status != state.last_status:
                logging.info("%s: %s", folder, status)
                state.last_status = status

            if args.ready_timeout > 0 and now - state.first_seen >= args.ready_timeout:
                message = f"{folder}: not ready after {args.ready_timeout:.0f} seconds"
                if args.once:
                    logging.error(message)
                    return 1
                if now - state.last_timeout_warning >= args.ready_timeout:
                    logging.warning(message)
                    state.last_timeout_warning = now

            if not is_ready(job, state, args):
                continue

            try:
                process_job(job, args)
            except Exception:
                logging.exception("Failed to process %s", folder)
                if args.once:
                    return 1
                continue

            processed.add(key)
            states.pop(key, None)

        if args.once:
            remaining = [
                folder
                for folder in candidate_folders
                if folder_key(folder) not in processed
            ]
            if not remaining:
                return 0

        time.sleep(args.poll_interval)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    try:
        return run(args)
    except KeyboardInterrupt:
        logging.info("Stopped by user")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
