from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import csv
import json


REPORT_HISTORY_LIMIT = 10
LOG_FILE_LIMIT = 10

REPORT_FIELDS = [
    "timestamp",
    "code",
    "status",
    "image_path",
    "product_url",
    "expected_name",
    "actual_name",
    "message",
]


@dataclass
class ReportRecord:
    code: str
    status: str
    image_path: str = ""
    product_url: str = ""
    expected_name: str = ""
    actual_name: str = ""
    message: str = ""
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat(timespec="seconds")


class ReportWriter:
    def __init__(self, report_dir: str | Path) -> None:
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._prune_old_files()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.csv_path = self.report_dir / f"upload-report-{stamp}.csv"
        self.json_path = self.report_dir / f"upload-report-{stamp}.json"
        self.records: list[ReportRecord] = []

    def add(self, record: ReportRecord) -> None:
        self.records.append(record)
        self.save()

    def save(self) -> None:
        with self.csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
            writer.writeheader()
            for record in self.records:
                writer.writerow(asdict(record))

        with self.json_path.open("w", encoding="utf-8") as handle:
            json.dump([asdict(record) for record in self.records], handle, ensure_ascii=False, indent=2)

        self._prune_old_files()

    def _prune_old_files(self) -> None:
        self._prune_report_files()
        self._prune_matching_files("*.log", LOG_FILE_LIMIT)

    def _prune_report_files(self) -> None:
        report_files = [
            path
            for pattern in ("upload-report-*.csv", "upload-report-*.json")
            for path in self.report_dir.glob(pattern)
            if path.is_file()
        ]
        grouped: dict[str, list[Path]] = {}
        for path in report_files:
            grouped.setdefault(path.stem, []).append(path)

        groups = sorted(
            grouped.items(),
            key=lambda item: max(path.stat().st_mtime for path in item[1]),
            reverse=True,
        )
        for _stem, files in groups[REPORT_HISTORY_LIMIT:]:
            for path in files:
                self._unlink_quietly(path)

    def _prune_matching_files(self, pattern: str, limit: int) -> None:
        files = sorted(
            (path for path in self.report_dir.glob(pattern) if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in files[limit:]:
            self._unlink_quietly(path)

    def _unlink_quietly(self, path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass
