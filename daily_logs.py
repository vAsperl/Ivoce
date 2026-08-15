"""Daily, per-run log files and retention cleanup."""

from __future__ import annotations

import logging
import re
import shutil
import threading
from datetime import date, datetime, timedelta
from pathlib import Path


_DATE_DIRECTORY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ExcludeLoggerFilter(logging.Filter):
    def __init__(self, *prefixes: str):
        super().__init__()
        self.prefixes = prefixes

    def filter(self, record):
        return not any(
            record.name == prefix or record.name.startswith(prefix + ".")
            for prefix in self.prefixes
        )


class DailyLogHandler(logging.Handler):
    """Write to ``root/YYYY-MM-DD/name-HH-MM-SS.log``, rolling at midnight."""

    def __init__(self, root, name: str, encoding: str = "utf-8"):
        super().__init__()
        self.root = Path(root)
        self.name = name
        self.encoding = encoding
        self._day = None
        self._file_handler = None
        self._lock = threading.RLock()

    def _handler_for_now(self):
        now = datetime.now().astimezone()
        current_day = now.date()
        if self._file_handler is not None and self._day == current_day:
            return self._file_handler
        if self._file_handler is not None:
            self._file_handler.close()
        day_directory = self.root / current_day.isoformat()
        day_directory.mkdir(parents=True, exist_ok=True)
        filename = f"{self.name}-{now:%H-%M-%S}.log"
        self._file_handler = logging.FileHandler(
            day_directory / filename, encoding=self.encoding, mode="a"
        )
        self._day = current_day
        return self._file_handler

    def emit(self, record):
        try:
            with self._lock:
                target = self._handler_for_now()
                target.setFormatter(self.formatter)
                target.emit(record)
        except Exception:
            self.handleError(record)

    def flush(self):
        with self._lock:
            if self._file_handler is not None:
                self._file_handler.flush()

    def close(self):
        with self._lock:
            if self._file_handler is not None:
                self._file_handler.close()
                self._file_handler = None
        super().close()


def remove_expired_log_folders(root, retention_days: int = 30, today=None):
    """Remove only valid dated child folders older than the retention window."""
    root = Path(root)
    if retention_days < 1 or not root.is_dir():
        return []
    today = today or date.today()
    cutoff = today - timedelta(days=retention_days)
    removed = []
    for child in root.iterdir():
        if not child.is_dir() or not _DATE_DIRECTORY.fullmatch(child.name):
            continue
        try:
            folder_day = date.fromisoformat(child.name)
        except ValueError:
            continue
        if folder_day < cutoff:
            shutil.rmtree(child)
            removed.append(child)
    return removed
