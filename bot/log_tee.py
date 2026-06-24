"""Tee stdout to a timestamped log file (polybot5m-style)."""

from __future__ import annotations

import atexit
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

_log_file_timestamp: LogFileLineTimestamp | None = None
_log_file_atexit_registered = False


class Tee:
    """Write to multiple streams (stdout + log file)."""

    def __init__(self, *streams: TextIO) -> None:
        self._streams = streams

    def write(self, s: str) -> None:
        for st in self._streams:
            try:
                st.write(s)
                st.flush()
            except OSError:
                pass

    def flush(self) -> None:
        for st in self._streams:
            try:
                st.flush()
            except OSError:
                pass


class LogFileLineTimestamp:
    """Prefix each full line written to the log file with UTC ISO time (ms)."""

    def __init__(self, raw: TextIO) -> None:
        self._raw = raw
        self._buf = ""

    def write(self, s: str) -> None:
        if not s:
            return
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._raw.write(self._prefix_line(line) + "\n")

    def flush(self) -> None:
        self._raw.flush()

    def flush_pending_line(self) -> None:
        if not self._buf:
            return
        self._raw.write(self._prefix_line(self._buf) + "\n")
        self._buf = ""
        self._raw.flush()

    @staticmethod
    def _prefix_line(line: str) -> str:
        ts = datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return f"{ts}   {line}"


def _flush_log_pending_line() -> None:
    global _log_file_timestamp
    if _log_file_timestamp is not None:
        try:
            _log_file_timestamp.flush_pending_line()
        except OSError:
            pass


def setup_log_file(log_path: Path | None) -> None:
    """Tee stdout to log_path; append session header on each startup."""
    global _log_file_timestamp, _log_file_atexit_registered
    if not log_path:
        return
    try:
        if isinstance(sys.stdout, Tee):
            try:
                sys.stdout.flush()
            except OSError:
                pass
            sys.stdout = sys.__stdout__
        if _log_file_timestamp is not None:
            try:
                _log_file_timestamp.flush_pending_line()
                _log_file_timestamp._raw.close()
            except OSError:
                pass
            _log_file_timestamp = None

        log_path.parent.mkdir(parents=True, exist_ok=True)
        f = open(log_path, "a", encoding="utf-8")
        ts = datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        f.write(f"\n--- session started {ts} ---\n")
        f.flush()
        _log_file_timestamp = LogFileLineTimestamp(f)
        if not _log_file_atexit_registered:
            atexit.register(_flush_log_pending_line)
            _log_file_atexit_registered = True
        sys.stdout = Tee(sys.__stdout__, _log_file_timestamp)
    except OSError as e:
        print(f"  Warning: could not open log file {log_path}: {e}", file=sys.__stderr__)
