from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import statistics
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


CSV_SUFFIX = ".csv"
IMAGE_SUFFIXES = {".jpg", ".jpeg"}
DEFAULT_RESULT_CODE = "23"
VALID_RESULT_MODES = {"fixed", "random_row", "random_file"}
VALID_SCENARIOS = {
    "normal_return",
    "delayed_return",
    "no_return",
    "empty_csv",
    "missing_is_pass_column",
    "partial_rows",
    "malformed_csv",
}
SUMMARY_FIELDS = (
    "run_id",
    "folder_name",
    "folder_path",
    "scenario",
    "status",
    "error",
    "discovered_at",
    "files_stable_at",
    "processing_started_at",
    "return_due_at",
    "return_started_at",
    "return_completed_at",
    "discovery_to_stable_seconds",
    "discovery_to_return_seconds",
    "processing_seconds",
    "configured_delay_seconds",
    "csv_count",
    "image_count",
    "rows_read",
    "rows_returned",
    "returned_file_count",
    "is_pass_22_count",
    "is_pass_23_count",
    "is_pass_other_count",
    "return_paths",
)


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
    first_seen_at: str
    stable_at: str | None = None
    stable_mono: float | None = None
    last_status: str = ""
    last_timeout_warning: float = 0.0


@dataclass(frozen=True)
class WatchConfig:
    poll_interval_seconds: float = 1.0
    settle_seconds: float = 2.0
    ready_timeout_seconds: float = 300.0
    allow_no_images: bool = False


@dataclass(frozen=True)
class InputConfig:
    input_dir: Path
    return_dir: Path
    preserve_folder: bool = False
    overwrite: bool = False


@dataclass(frozen=True)
class ResultConfig:
    mode: str = "fixed"
    fixed_code: str = DEFAULT_RESULT_CODE
    weights: dict[str, float] = field(default_factory=lambda: {"22": 50.0, "23": 50.0})


@dataclass(frozen=True)
class ScenarioCase:
    name: str
    weight: float = 1.0
    min_delay_seconds: float = 0.0
    max_delay_seconds: float = 0.0
    partial_row_ratio: float = 0.5


@dataclass(frozen=True)
class ScenarioConfig:
    cases: tuple[ScenarioCase, ...] = (ScenarioCase(name="normal_return"),)


@dataclass(frozen=True)
class ReportConfig:
    output_dir: Path = Path("reports")
    create_run_subdir: bool = True
    write_jsonl: bool = True
    write_summary_csv: bool = True
    write_markdown_report: bool = True


@dataclass(frozen=True)
class RunConfig:
    once: bool = False
    random_seed: int | None = None
    log_level: str = "INFO"


@dataclass(frozen=True)
class AppConfig:
    input: InputConfig
    watch: WatchConfig
    result: ResultConfig
    scenario: ScenarioConfig
    report: ReportConfig
    run: RunConfig
    config_path: Path | None = None


@dataclass(frozen=True)
class CsvMetadata:
    fieldnames: list[str]
    rows: list[dict[str, str]]
    encoding: str
    lineterminator: str
    dialect: type[csv.Dialect] | csv.Dialect


@dataclass(frozen=True)
class CsvWriteResult:
    source_csv: Path
    destination_csv: Path
    rows_read: int
    rows_returned: int
    code_counts: Counter[str]


@dataclass
class PendingReturn:
    key: str
    job: MachineJob
    state: FolderState
    scenario: ScenarioCase
    processing_started_at: str
    processing_started_mono: float
    delay_seconds: float
    due_at: str
    due_mono: float
    finalized: bool = False


class Reporter:
    def __init__(self, config: AppConfig, run_id: str) -> None:
        self.config = config
        self.run_id = run_id
        self.started_at = now_iso()
        self.summary_rows: list[dict[str, Any]] = []

        output_dir = config.report.output_dir
        if config.report.create_run_subdir:
            output_dir = output_dir / run_id
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.events_path = self.output_dir / "events.jsonl"
        self.summary_path = self.output_dir / "summary.csv"
        self.report_path = self.output_dir / "report.md"

        if config.report.write_summary_csv and not self.summary_path.exists():
            with self.summary_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=SUMMARY_FIELDS)
                writer.writeheader()

    def event(self, event_type: str, **payload: Any) -> None:
        record = {
            "run_id": self.run_id,
            "event": event_type,
            "at": now_iso(),
            **payload,
        }
        logging.debug("event=%s payload=%s", event_type, payload)
        if not self.config.report.write_jsonl:
            return
        with self.events_path.open("a", encoding="utf-8") as file:
            json.dump(record, file, ensure_ascii=False, default=json_default)
            file.write("\n")

    def summary(self, row: dict[str, Any]) -> None:
        complete_row = {field: row.get(field, "") for field in SUMMARY_FIELDS}
        complete_row["run_id"] = self.run_id
        self.summary_rows.append(complete_row)

        if self.config.report.write_summary_csv:
            with self.summary_path.open("a", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=SUMMARY_FIELDS)
                writer.writerow({key: stringify_summary_value(value) for key, value in complete_row.items()})

        if self.config.report.write_markdown_report:
            self.write_markdown_report()

    def write_markdown_report(self, final: bool = False, pending_count: int = 0) -> None:
        if not self.config.report.write_markdown_report:
            return

        rows = self.summary_rows
        total = len(rows)
        status_counts = Counter(row["status"] for row in rows)
        scenario_counts = Counter(row["scenario"] for row in rows)
        returned_rows = sum(to_int(row["rows_returned"]) for row in rows)
        rows_read = sum(to_int(row["rows_read"]) for row in rows)
        code_22 = sum(to_int(row["is_pass_22_count"]) for row in rows)
        code_23 = sum(to_int(row["is_pass_23_count"]) for row in rows)
        code_other = sum(to_int(row["is_pass_other_count"]) for row in rows)

        lines = [
            "# Machine Interface Test Report",
            "",
            f"- Run ID: `{self.run_id}`",
            f"- Started at: `{self.started_at}`",
            f"- Last updated: `{now_iso()}`",
            f"- Status: `{'final' if final else 'running'}`",
            f"- Input directory: `{self.config.input.input_dir}`",
            f"- Return directory: `{self.config.input.return_dir}`",
            f"- Config: `{self.config.config_path or 'CLI/defaults'}`",
            "",
            "## Summary",
            "",
            f"- Finalized jobs: `{total}`",
            f"- Pending delayed returns: `{pending_count}`",
            f"- Rows read: `{rows_read}`",
            f"- Rows returned: `{returned_rows}`",
            f"- is_pass 22 count: `{code_22}`",
            f"- is_pass 23 count: `{code_23}`",
            f"- is_pass other count: `{code_other}`",
            "",
            "## Status Counts",
            "",
            *format_counter_table(status_counts, "Status"),
            "",
            "## Scenario Counts",
            "",
            *format_counter_table(scenario_counts, "Scenario"),
            "",
            "## Timing",
            "",
            *format_timing_section(rows),
            "",
            "## Recent Non-Success Rows",
            "",
            *format_recent_failures(rows),
            "",
            "## Output Files",
            "",
            f"- Events JSONL: `{self.events_path}`",
            f"- Summary CSV: `{self.summary_path}`",
            f"- Markdown report: `{self.report_path}`",
            "",
        ]
        self.report_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run machine CSV/JPG interface tests and generate reports."
    )
    parser.add_argument(
        "--config",
        default=Path("config.toml"),
        type=Path,
        help="TOML config file. Default: config.toml if present.",
    )
    parser.add_argument("--input-dir", type=Path, help="Override config input.input_dir.")
    parser.add_argument("--return-dir", type=Path, help="Override config input.return_dir.")
    parser.add_argument("--result-code", help="Override result mode to fixed and use this code.")
    parser.add_argument("--poll-interval", type=float, help="Override watch.poll_interval_seconds.")
    parser.add_argument("--settle-seconds", type=float, help="Override watch.settle_seconds.")
    parser.add_argument("--ready-timeout", type=float, help="Override watch.ready_timeout_seconds.")
    parser.add_argument("--once", action="store_true", default=None, help="Process ready folders and exit.")
    parser.add_argument("--overwrite", action="store_true", default=None, help="Overwrite returned CSV files.")
    parser.add_argument("--allow-no-images", action="store_true", default=None, help="Allow CSV processing without JPG/JPEG files.")
    parser.add_argument("--preserve-folder", action="store_true", default=None, help="Return under return-dir/<timestamp-folder>/.")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Override run.log_level.",
    )
    return parser.parse_args(argv)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_config(args: argparse.Namespace) -> AppConfig:
    config_path = args.config
    config_data: dict[str, Any] = {}
    config_base = Path.cwd()

    if config_path.exists():
        config_path = config_path.resolve()
        config_base = config_path.parent
        with config_path.open("rb") as file:
            config_data = tomllib.load(file)
    elif args.config != Path("config.toml"):
        raise FileNotFoundError(f"Config file not found: {args.config}")
    else:
        config_path = None

    input_section = dict(config_data.get("input", {}))
    watch_section = dict(config_data.get("watch", {}))
    result_section = dict(config_data.get("result", {}))
    scenario_section = dict(config_data.get("scenario", {}))
    report_section = dict(config_data.get("report", {}))
    run_section = dict(config_data.get("run", {}))

    input_dir = args.input_dir or path_from_config(input_section.get("input_dir"), config_base)
    return_dir = args.return_dir or path_from_config(input_section.get("return_dir"), config_base)
    if input_dir is None:
        raise ValueError("Missing input directory. Set input.input_dir in config.toml or pass --input-dir.")
    if return_dir is None:
        raise ValueError("Missing return directory. Set input.return_dir in config.toml or pass --return-dir.")

    preserve_folder = bool_value(args.preserve_folder, input_section.get("preserve_folder", False))
    overwrite = bool_value(args.overwrite, input_section.get("overwrite", False))
    allow_no_images = bool_value(args.allow_no_images, watch_section.get("allow_no_images", False))

    result_mode = str(result_section.get("mode", "fixed"))
    fixed_code = str(result_section.get("fixed_code", DEFAULT_RESULT_CODE))
    if args.result_code is not None:
        result_mode = "fixed"
        fixed_code = str(args.result_code)
    if result_mode not in VALID_RESULT_MODES:
        raise ValueError(f"Invalid result.mode: {result_mode}. Valid values: {sorted(VALID_RESULT_MODES)}")

    result_weights = result_section.get("weights", {"22": 50, "23": 50})
    if not isinstance(result_weights, dict):
        raise ValueError("result.weights must be a TOML table, for example [result.weights].")
    weights = {str(code): float(weight) for code, weight in result_weights.items() if float(weight) > 0}
    if not weights:
        raise ValueError("result.weights must contain at least one positive weight.")

    scenario_cases = parse_scenario_cases(scenario_section)

    output_dir = path_from_config(report_section.get("output_dir", "reports"), config_base)
    assert output_dir is not None

    random_seed = run_section.get("random_seed")
    if random_seed is not None:
        random_seed = int(random_seed)

    run_once = bool_value(args.once, run_section.get("once", False))
    log_level = str(args.log_level or run_section.get("log_level", "INFO")).upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("run.log_level must be DEBUG, INFO, WARNING, or ERROR.")

    return AppConfig(
        input=InputConfig(
            input_dir=input_dir,
            return_dir=return_dir,
            preserve_folder=preserve_folder,
            overwrite=overwrite,
        ),
        watch=WatchConfig(
            poll_interval_seconds=float(args.poll_interval if args.poll_interval is not None else watch_section.get("poll_interval_seconds", 1.0)),
            settle_seconds=float(args.settle_seconds if args.settle_seconds is not None else watch_section.get("settle_seconds", 2.0)),
            ready_timeout_seconds=float(args.ready_timeout if args.ready_timeout is not None else watch_section.get("ready_timeout_seconds", 300.0)),
            allow_no_images=allow_no_images,
        ),
        result=ResultConfig(
            mode=result_mode,
            fixed_code=fixed_code,
            weights=weights,
        ),
        scenario=ScenarioConfig(cases=scenario_cases),
        report=ReportConfig(
            output_dir=output_dir,
            create_run_subdir=bool(report_section.get("create_run_subdir", True)),
            write_jsonl=bool(report_section.get("write_jsonl", True)),
            write_summary_csv=bool(report_section.get("write_summary_csv", True)),
            write_markdown_report=bool(report_section.get("write_markdown_report", True)),
        ),
        run=RunConfig(
            once=run_once,
            random_seed=random_seed,
            log_level=log_level,
        ),
        config_path=config_path,
    )


def parse_scenario_cases(section: dict[str, Any]) -> tuple[ScenarioCase, ...]:
    raw_cases = section.get("cases", [{"name": "normal_return", "weight": 1}])
    if not isinstance(raw_cases, list):
        raise ValueError("scenario.cases must be configured with [[scenario.cases]] entries.")

    cases: list[ScenarioCase] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("Each scenario case must be a TOML table.")
        if raw_case.get("enabled", True) is False:
            continue

        name = str(raw_case.get("name", "")).strip()
        if name not in VALID_SCENARIOS:
            raise ValueError(f"Invalid scenario case: {name}. Valid values: {sorted(VALID_SCENARIOS)}")

        weight = float(raw_case.get("weight", 1.0))
        if weight <= 0:
            continue

        min_delay = float(raw_case.get("min_delay_seconds", 0.0))
        max_delay = float(raw_case.get("max_delay_seconds", min_delay))
        if min_delay < 0 or max_delay < 0 or max_delay < min_delay:
            raise ValueError(f"Invalid delay range for scenario {name}.")

        partial_ratio = float(raw_case.get("partial_row_ratio", 0.5))
        if partial_ratio < 0 or partial_ratio > 1:
            raise ValueError(f"partial_row_ratio for {name} must be between 0 and 1.")

        cases.append(
            ScenarioCase(
                name=name,
                weight=weight,
                min_delay_seconds=min_delay,
                max_delay_seconds=max_delay,
                partial_row_ratio=partial_ratio,
            )
        )

    if not cases:
        raise ValueError("At least one enabled scenario case with positive weight is required.")
    return tuple(cases)


def path_from_config(value: Any, base: Path) -> Path | None:
    if value is None or value == "":
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def bool_value(cli_value: bool | None, config_value: Any) -> bool:
    if cli_value is not None:
        return bool(cli_value)
    return bool(config_value)


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


def is_ready(job: MachineJob, state: FolderState, config: AppConfig) -> bool:
    if not job.csv_files:
        return False
    if not config.watch.allow_no_images and not job.image_files:
        return False
    return (time.monotonic() - state.stable_since) >= config.watch.settle_seconds


def return_csv_path(source_folder: Path, csv_path: Path, config: AppConfig) -> Path:
    if config.input.preserve_folder:
        return config.input.return_dir / source_folder.name / csv_path.name
    return config.input.return_dir / csv_path.name


def outputs_already_exist(job: MachineJob, config: AppConfig) -> bool:
    if config.input.overwrite or not job.csv_files:
        return False
    return all(return_csv_path(job.folder, csv_path, config).exists() for csv_path in job.csv_files)


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


def read_csv_metadata(source_csv: Path) -> CsvMetadata:
    encoding = detect_encoding(source_csv)
    lineterminator = detect_lineterminator(source_csv)
    dialect = detect_dialect(source_csv, encoding)

    with source_csv.open("r", encoding=encoding, newline="") as source_file:
        reader = csv.DictReader(source_file, dialect=dialect)
        fieldnames = reader.fieldnames
        if not fieldnames:
            raise ValueError(f"{source_csv} has no CSV header")
        rows = list(reader)

    return CsvMetadata(
        fieldnames=list(fieldnames),
        rows=rows,
        encoding=encoding,
        lineterminator=lineterminator,
        dialect=dialect,
    )


def choose_weighted(mapping: dict[str, float], rng: random.Random) -> str:
    choices = list(mapping.keys())
    weights = list(mapping.values())
    return rng.choices(choices, weights=weights, k=1)[0]


def choose_result_codes(row_count: int, config: AppConfig, rng: random.Random) -> list[str]:
    if config.result.mode == "fixed":
        return [config.result.fixed_code] * row_count
    if config.result.mode == "random_file":
        return [choose_weighted(config.result.weights, rng)] * row_count
    if config.result.mode == "random_row":
        return [choose_weighted(config.result.weights, rng) for _ in range(row_count)]
    raise ValueError(f"Unsupported result mode: {config.result.mode}")


def choose_scenario(config: AppConfig, rng: random.Random) -> ScenarioCase:
    choices = list(config.scenario.cases)
    weights = [case.weight for case in choices]
    return rng.choices(choices, weights=weights, k=1)[0]


def choose_delay_seconds(scenario: ScenarioCase, rng: random.Random) -> float:
    if scenario.max_delay_seconds <= scenario.min_delay_seconds:
        return scenario.min_delay_seconds
    return rng.uniform(scenario.min_delay_seconds, scenario.max_delay_seconds)


def atomic_write(destination: Path, writer: Any) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(
        f".{destination.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    )
    try:
        writer(temp_path)
        os.replace(temp_path, destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def write_empty_csv(destination_csv: Path) -> None:
    def writer(temp_path: Path) -> None:
        with temp_path.open("wb") as file:
            file.flush()
            os.fsync(file.fileno())

    atomic_write(destination_csv, writer)


def write_malformed_csv(destination_csv: Path) -> None:
    def writer(temp_path: Path) -> None:
        with temp_path.open("w", encoding="utf-8", newline="") as file:
            file.write('this,is,not,a,valid,csv\n"unterminated field,23\n')
            file.flush()
            os.fsync(file.fileno())

    atomic_write(destination_csv, writer)


def write_modified_csv(
    source_csv: Path,
    destination_csv: Path,
    scenario: ScenarioCase,
    config: AppConfig,
    rng: random.Random,
) -> CsvWriteResult:
    if scenario.name == "empty_csv":
        rows_read = read_rows_count(source_csv)
        write_empty_csv(destination_csv)
        return CsvWriteResult(
            source_csv=source_csv,
            destination_csv=destination_csv,
            rows_read=rows_read,
            rows_returned=0,
            code_counts=Counter(),
        )

    if scenario.name == "malformed_csv":
        rows_read = read_rows_count(source_csv)
        write_malformed_csv(destination_csv)
        return CsvWriteResult(
            source_csv=source_csv,
            destination_csv=destination_csv,
            rows_read=rows_read,
            rows_returned=0,
            code_counts=Counter(),
        )

    metadata = read_csv_metadata(source_csv)
    rows = list(metadata.rows)
    rows_read = len(rows)

    if "is_pass" not in metadata.fieldnames and scenario.name != "missing_is_pass_column":
        raise ValueError(f"{source_csv} does not contain required column: is_pass")

    if scenario.name == "partial_rows":
        partial_count = calculate_partial_count(len(rows), scenario.partial_row_ratio)
        rows = rows[:partial_count]

    code_counts: Counter[str] = Counter()
    result_codes = choose_result_codes(len(rows), config, rng)
    for row, code in zip(rows, result_codes, strict=True):
        row["is_pass"] = code
        code_counts[code] += 1

    fieldnames = list(metadata.fieldnames)
    if scenario.name == "missing_is_pass_column":
        fieldnames = [fieldname for fieldname in fieldnames if fieldname != "is_pass"]
        for row in rows:
            row.pop("is_pass", None)
        code_counts = Counter()

    def writer(temp_path: Path) -> None:
        with temp_path.open("w", encoding=metadata.encoding, newline="") as destination_file:
            csv_writer = csv.DictWriter(
                destination_file,
                fieldnames=fieldnames,
                dialect=metadata.dialect,
                lineterminator=metadata.lineterminator,
                extrasaction="ignore",
            )
            csv_writer.writeheader()
            csv_writer.writerows(rows)
            destination_file.flush()
            os.fsync(destination_file.fileno())

    atomic_write(destination_csv, writer)

    return CsvWriteResult(
        source_csv=source_csv,
        destination_csv=destination_csv,
        rows_read=rows_read,
        rows_returned=len(rows),
        code_counts=code_counts,
    )


def read_rows_count(source_csv: Path) -> int:
    try:
        return len(read_csv_metadata(source_csv).rows)
    except Exception:
        return 0


def calculate_partial_count(row_count: int, ratio: float) -> int:
    if row_count <= 0 or ratio <= 0:
        return 0
    return max(1, min(row_count, int(row_count * ratio)))


def execute_return(
    job: MachineJob,
    state: FolderState,
    scenario: ScenarioCase,
    config: AppConfig,
    reporter: Reporter,
    rng: random.Random,
    processing_started_at: str,
    processing_started_mono: float,
    delay_seconds: float = 0.0,
    return_due_at: str = "",
) -> None:
    return_started_at = now_iso()
    reporter.event(
        "return_started",
        folder=str(job.folder),
        scenario=scenario.name,
        csv_count=len(job.csv_files),
        image_count=len(job.image_files),
    )

    write_results: list[CsvWriteResult] = []
    try:
        for csv_path in job.csv_files:
            destination_csv = return_csv_path(job.folder, csv_path, config)
            if destination_csv.exists() and not config.input.overwrite:
                raise FileExistsError(f"Return CSV already exists: {destination_csv}")
            write_results.append(
                write_modified_csv(csv_path, destination_csv, scenario, config, rng)
            )
    except Exception as exc:
        logging.exception("Failed to return CSV for %s", job.folder)
        reporter.event(
            "return_failed",
            folder=str(job.folder),
            scenario=scenario.name,
            error=str(exc),
        )
        failed_at_mono = time.monotonic()
        reporter.summary(
            build_summary_row(
                job=job,
                state=state,
                scenario=scenario,
                status="failed",
                error=str(exc),
                processing_started_at=processing_started_at,
                return_due_at=return_due_at,
                return_started_at=return_started_at,
                return_completed_at=now_iso(),
                processing_seconds=failed_at_mono - processing_started_mono,
                configured_delay_seconds=delay_seconds,
                write_results=write_results,
                return_completed_mono=failed_at_mono,
            )
        )
        return

    return_completed_at = now_iso()
    return_completed_mono = time.monotonic()
    processing_seconds = return_completed_mono - processing_started_mono
    reporter.event(
        "return_completed",
        folder=str(job.folder),
        scenario=scenario.name,
        processing_seconds=round(processing_seconds, 6),
        return_paths=[str(result.destination_csv) for result in write_results],
    )
    reporter.summary(
        build_summary_row(
            job=job,
            state=state,
            scenario=scenario,
            status="returned",
            error="",
            processing_started_at=processing_started_at,
            return_due_at=return_due_at,
            return_started_at=return_started_at,
            return_completed_at=return_completed_at,
            processing_seconds=processing_seconds,
            configured_delay_seconds=delay_seconds,
            write_results=write_results,
            return_completed_mono=return_completed_mono,
        )
    )
    logging.info(
        "Returned %d CSV file(s) for %s using scenario=%s",
        len(write_results),
        job.folder,
        scenario.name,
    )


def execute_no_return(
    job: MachineJob,
    state: FolderState,
    scenario: ScenarioCase,
    reporter: Reporter,
    processing_started_at: str,
    processing_started_mono: float,
) -> None:
    processing_seconds = time.monotonic() - processing_started_mono
    reporter.event(
        "no_return_simulated",
        folder=str(job.folder),
        scenario=scenario.name,
        csv_count=len(job.csv_files),
        image_count=len(job.image_files),
    )
    reporter.summary(
        build_summary_row(
            job=job,
            state=state,
            scenario=scenario,
            status="simulated_no_return",
            error="CSV intentionally not returned by test scenario",
            processing_started_at=processing_started_at,
            return_due_at="",
            return_started_at="",
            return_completed_at="",
            processing_seconds=processing_seconds,
            configured_delay_seconds=0.0,
            write_results=[],
        )
    )
    logging.warning("Simulated no return for %s", job.folder)


def build_summary_row(
    job: MachineJob,
    state: FolderState,
    scenario: ScenarioCase,
    status: str,
    error: str,
    processing_started_at: str,
    return_due_at: str,
    return_started_at: str,
    return_completed_at: str,
    processing_seconds: float,
    configured_delay_seconds: float,
    write_results: list[CsvWriteResult],
    return_completed_mono: float | None = None,
) -> dict[str, Any]:
    rows_read = sum(result.rows_read for result in write_results)
    rows_returned = sum(result.rows_returned for result in write_results)
    code_counts: Counter[str] = Counter()
    for result in write_results:
        code_counts.update(result.code_counts)

    discovery_to_stable = ""
    if state.stable_mono is not None:
        discovery_to_stable = round(state.stable_mono - state.first_seen, 6)

    discovery_to_return = ""
    if return_completed_at and return_completed_mono is not None:
        discovery_to_return = round(return_completed_mono - state.first_seen, 6)

    return_paths = [str(result.destination_csv) for result in write_results]
    other_count = sum(count for code, count in code_counts.items() if code not in {"22", "23"})

    return {
        "folder_name": job.folder.name,
        "folder_path": str(job.folder),
        "scenario": scenario.name,
        "status": status,
        "error": error,
        "discovered_at": state.first_seen_at,
        "files_stable_at": state.stable_at or "",
        "processing_started_at": processing_started_at,
        "return_due_at": return_due_at,
        "return_started_at": return_started_at,
        "return_completed_at": return_completed_at,
        "discovery_to_stable_seconds": discovery_to_stable,
        "discovery_to_return_seconds": discovery_to_return,
        "processing_seconds": round(processing_seconds, 6),
        "configured_delay_seconds": round(configured_delay_seconds, 6),
        "csv_count": len(job.csv_files),
        "image_count": len(job.image_files),
        "rows_read": rows_read,
        "rows_returned": rows_returned,
        "returned_file_count": len(write_results),
        "is_pass_22_count": code_counts.get("22", 0),
        "is_pass_23_count": code_counts.get("23", 0),
        "is_pass_other_count": other_count,
        "return_paths": json.dumps(return_paths, ensure_ascii=False),
    }


def folder_key(folder: Path) -> str:
    return str(folder.resolve())


def run(config: AppConfig) -> int:
    if not config.input.input_dir.exists() or not config.input.input_dir.is_dir():
        logging.error("Input directory does not exist or is not a directory: %s", config.input.input_dir)
        return 2

    config.input.return_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    reporter = Reporter(config, run_id)
    rng = random.Random(config.run.random_seed)
    states: dict[str, FolderState] = {}
    processed: set[str] = set()
    pending_returns: list[PendingReturn] = []
    exit_code = 0

    logging.info("Input directory: %s", config.input.input_dir)
    logging.info("Return directory: %s", config.input.return_dir)
    logging.info("Report directory: %s", reporter.output_dir)
    logging.info("Mode: %s", "once" if config.run.once else "watch")
    logging.info("Result mode: %s", config.result.mode)
    reporter.event(
        "run_started",
        input_dir=str(config.input.input_dir),
        return_dir=str(config.input.return_dir),
        report_dir=str(reporter.output_dir),
        mode="once" if config.run.once else "watch",
        result_mode=config.result.mode,
        scenarios=[case.name for case in config.scenario.cases],
    )

    try:
        while True:
            now = time.monotonic()
            finalize_due_pending_returns(pending_returns, now, config, reporter, rng)

            candidate_folders = list_candidate_folders(config.input.input_dir)
            if not candidate_folders and config.run.once and not pending_returns:
                logging.warning("No timestamp folders or CSV files found in %s", config.input.input_dir)
                break

            for folder in candidate_folders:
                key = folder_key(folder)
                if key in processed:
                    continue

                job = scan_job(folder)
                state = states.get(key)
                if state is None:
                    state = FolderState(
                        snapshot=snapshot_job(job),
                        stable_since=now,
                        first_seen=now,
                        first_seen_at=now_iso(),
                    )
                    states[key] = state
                    reporter.event("folder_discovered", folder=str(folder))
                else:
                    snapshot = snapshot_job(job)
                    if snapshot != state.snapshot:
                        state.snapshot = snapshot
                        state.stable_since = now
                        state.stable_at = None
                        state.stable_mono = None

                if outputs_already_exist(job, config):
                    logging.info("Skipping %s because returned CSV already exists", folder)
                    reporter.event("skipped_output_exists", folder=str(folder))
                    reporter.summary(
                        build_summary_row(
                            job=job,
                            state=state,
                            scenario=ScenarioCase(name="normal_return"),
                            status="skipped_output_exists",
                            error="Returned CSV already exists and overwrite is false",
                            processing_started_at="",
                            return_due_at="",
                            return_started_at="",
                            return_completed_at="",
                            processing_seconds=0.0,
                            configured_delay_seconds=0.0,
                            write_results=[],
                        )
                    )
                    processed.add(key)
                    states.pop(key, None)
                    continue

                status = status_for_job(job, config.watch.allow_no_images)
                if status != state.last_status:
                    logging.info("%s: %s", folder, status)
                    reporter.event("folder_status", folder=str(folder), status=status)
                    state.last_status = status

                if is_ready(job, state, config):
                    if state.stable_at is None:
                        state.stable_at = now_iso()
                        state.stable_mono = now
                        reporter.event("folder_ready", folder=str(folder))
                    start_job(job, state, key, config, reporter, rng, pending_returns, processed)
                    states.pop(key, None)
                    continue

                handle_ready_timeout(job, state, config, reporter)

            if config.run.once:
                remaining = [
                    folder
                    for folder in candidate_folders
                    if folder_key(folder) not in processed
                ]
                if not remaining and not pending_returns:
                    break

            time.sleep(config.watch.poll_interval_seconds)
    except KeyboardInterrupt:
        logging.info("Stopped by user")
        reporter.event("run_stopped_by_user")
        exit_code = 130
    finally:
        for pending in pending_returns:
            if pending.finalized:
                continue
            pending.finalized = True
            reporter.event(
                "pending_return_interrupted",
                folder=str(pending.job.folder),
                scenario=pending.scenario.name,
                due_at=pending.due_at,
            )
            reporter.summary(
                build_summary_row(
                    job=pending.job,
                    state=pending.state,
                    scenario=pending.scenario,
                    status="interrupted_pending_return",
                    error="Program stopped before delayed return was due",
                    processing_started_at=pending.processing_started_at,
                    return_due_at=pending.due_at,
                    return_started_at="",
                    return_completed_at="",
                    processing_seconds=time.monotonic() - pending.processing_started_mono,
                    configured_delay_seconds=pending.delay_seconds,
                    write_results=[],
                )
            )
        reporter.event("run_finished", exit_code=exit_code)
        reporter.write_markdown_report(final=True, pending_count=0)
        logging.info("Report written to %s", reporter.report_path)

    return exit_code


def start_job(
    job: MachineJob,
    state: FolderState,
    key: str,
    config: AppConfig,
    reporter: Reporter,
    rng: random.Random,
    pending_returns: list[PendingReturn],
    processed: set[str],
) -> None:
    scenario = choose_scenario(config, rng)
    processing_started_at = now_iso()
    processing_started_mono = time.monotonic()
    processed.add(key)

    logging.info(
        "Processing %s with scenario=%s (%d CSV, %d image)",
        job.folder,
        scenario.name,
        len(job.csv_files),
        len(job.image_files),
    )
    reporter.event(
        "scenario_selected",
        folder=str(job.folder),
        scenario=scenario.name,
        csv_count=len(job.csv_files),
        image_count=len(job.image_files),
    )

    if scenario.name == "no_return":
        execute_no_return(job, state, scenario, reporter, processing_started_at, processing_started_mono)
        return

    if scenario.name == "delayed_return":
        delay_seconds = choose_delay_seconds(scenario, rng)
        due_mono = processing_started_mono + delay_seconds
        due_at = iso_from_monotonic_due(processing_started_mono, due_mono)
        pending_returns.append(
            PendingReturn(
                key=key,
                job=job,
                state=state,
                scenario=scenario,
                processing_started_at=processing_started_at,
                processing_started_mono=processing_started_mono,
                delay_seconds=delay_seconds,
                due_at=due_at,
                due_mono=due_mono,
            )
        )
        reporter.event(
            "return_scheduled",
            folder=str(job.folder),
            scenario=scenario.name,
            delay_seconds=round(delay_seconds, 6),
            due_at=due_at,
        )
        logging.warning(
            "Scheduled delayed return for %s in %.2f second(s)",
            job.folder,
            delay_seconds,
        )
        return

    execute_return(
        job=job,
        state=state,
        scenario=scenario,
        config=config,
        reporter=reporter,
        rng=rng,
        processing_started_at=processing_started_at,
        processing_started_mono=processing_started_mono,
    )


def finalize_due_pending_returns(
    pending_returns: list[PendingReturn],
    now_mono: float,
    config: AppConfig,
    reporter: Reporter,
    rng: random.Random,
) -> None:
    for pending in list(pending_returns):
        if pending.finalized or pending.due_mono > now_mono:
            continue

        pending.finalized = True
        reporter.event(
            "delayed_return_due",
            folder=str(pending.job.folder),
            scenario=pending.scenario.name,
            due_at=pending.due_at,
        )
        execute_return(
            job=pending.job,
            state=pending.state,
            scenario=pending.scenario,
            config=config,
            reporter=reporter,
            rng=rng,
            processing_started_at=pending.processing_started_at,
            processing_started_mono=pending.processing_started_mono,
            delay_seconds=pending.delay_seconds,
            return_due_at=pending.due_at,
        )
        pending_returns.remove(pending)


def handle_ready_timeout(
    job: MachineJob,
    state: FolderState,
    config: AppConfig,
    reporter: Reporter,
) -> None:
    timeout = config.watch.ready_timeout_seconds
    if timeout <= 0:
        return

    now = time.monotonic()
    if now - state.first_seen < timeout:
        return

    if now - state.last_timeout_warning < timeout:
        return

    message = f"{job.folder}: not ready after {timeout:.0f} seconds"
    if not job.csv_files:
        reason = "missing_csv"
    elif not config.watch.allow_no_images and not job.image_files:
        reason = "missing_images"
    else:
        reason = "files_not_stable"

    logging.warning(message)
    reporter.event(
        "folder_ready_timeout",
        folder=str(job.folder),
        reason=reason,
        timeout_seconds=timeout,
        csv_count=len(job.csv_files),
        image_count=len(job.image_files),
    )
    state.last_timeout_warning = now


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def iso_from_monotonic_due(start_mono: float, due_mono: float) -> str:
    delay = max(0.0, due_mono - start_mono)
    due_epoch = time.time() + delay
    return datetime.fromtimestamp(due_epoch).astimezone().isoformat(timespec="milliseconds")


def json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def stringify_summary_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def to_int(value: Any) -> int:
    try:
        if value == "":
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def to_float(value: Any) -> float | None:
    try:
        if value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def format_counter_table(counter: Counter[str], first_column: str) -> list[str]:
    if not counter:
        return ["No rows yet."]
    lines = [f"| {first_column} | Count |", "| --- | ---: |"]
    for key, count in sorted(counter.items()):
        lines.append(f"| `{key}` | {count} |")
    return lines


def format_timing_section(rows: list[dict[str, Any]]) -> list[str]:
    fields = (
        ("discovery_to_stable_seconds", "Discovery to stable"),
        ("discovery_to_return_seconds", "Discovery to return"),
        ("processing_seconds", "Processing"),
        ("configured_delay_seconds", "Configured delay"),
    )
    lines = ["| Metric | Min | Avg | Max |", "| --- | ---: | ---: | ---: |"]
    has_any = False
    for field_name, label in fields:
        values = [to_float(row.get(field_name)) for row in rows]
        clean_values = [value for value in values if value is not None]
        if not clean_values:
            continue
        has_any = True
        lines.append(
            f"| {label} | {min(clean_values):.3f}s | "
            f"{statistics.mean(clean_values):.3f}s | {max(clean_values):.3f}s |"
        )
    if not has_any:
        return ["No timing data yet."]
    return lines


def format_recent_failures(rows: list[dict[str, Any]]) -> list[str]:
    failures = [
        row
        for row in rows
        if row.get("status") != "returned"
    ][-10:]
    if not failures:
        return ["No failed or skipped rows yet."]

    lines = ["| Folder | Scenario | Status | Error |", "| --- | --- | --- | --- |"]
    for row in failures:
        lines.append(
            f"| `{row.get('folder_name', '')}` | `{row.get('scenario', '')}` | "
            f"`{row.get('status', '')}` | {row.get('error', '')} |"
        )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args)
    except Exception as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")
        logging.error("%s", exc)
        return 2

    configure_logging(config.run.log_level)
    return run(config)


if __name__ == "__main__":
    raise SystemExit(main())
