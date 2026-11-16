"""Find and search AutoCount log / exception files (read-only)."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..masking import Masker

LOG_SUFFIXES = {".log", ".txt"}
LOG_NAME_HINT = re.compile(r"(?i)(log|error|exception|trace|crash)")
ERROR_LINE = re.compile(r"(?i)\b(exception|error|failed|fatal)\b")
MAX_DEPTH = 4
MAX_FILES = 300  # newest files only
TAIL_BYTES = 1024 * 1024  # only the last 1 MB of each file is searched
CACHE_BUDGET = 64 * 1024 * 1024
MAX_LINE = 400

_TS_YMD = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?")
_TS_DMY = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}),?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*([AaPp][Mm]))?")
_TS_SEARCH_WIDTH = 48  # timestamps sit at the start of a log line


def parse_timestamp(line: str) -> dt.datetime | None:
    """Timestamp at the start of a log line. d/M/yyyy is read day-first (Malaysian locale)."""
    head = line[:_TS_SEARCH_WIDTH]
    m = _TS_YMD.search(head)
    try:
        if m:
            y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
            return dt.datetime(y, mo, d, h, mi, int(m.group(6) or 0))
        m = _TS_DMY.search(head)
        if m:
            d, mo, y, h, mi = (int(m.group(i)) for i in range(1, 6))
            if mo > 12 >= d:  # clearly month-first
                d, mo = mo, d
            ampm = (m.group(7) or "").lower()
            if ampm == "pm" and h < 12:
                h += 12
            elif ampm == "am" and h == 12:
                h = 0
            return dt.datetime(y, mo, d, h, mi, int(m.group(6) or 0))
    except ValueError:
        return None
    return None


@dataclass
class _Line:
    no: int  # 1-based line number in the file
    text: str
    ts: dt.datetime | None  # own timestamp, or the one inherited from the entry it belongs to
    starts_entry: bool  # True when the line carries its own timestamp


@dataclass
class LogHit:
    file: str  # display path (Windows user name masked)
    line_no: int
    line: str  # masked
    timestamp: str | None
    context: list[str] = field(default_factory=list)  # masked lines that follow (stack trace)

    @property
    def ref(self) -> str:
        return f"{self.file}:{self.line_no}"


@dataclass
class LogSearch:
    hits: list[LogHit]
    older_ignored: int  # matches found but older than the time window
    files_searched: int
    files_too_old: int = 0  # files not written to inside the time window (not searched)


def _decode(data: bytes, head: bytes) -> tuple[str, str]:
    if head.startswith(b"\xff\xfe"):
        return data.decode("utf-16-le", errors="replace"), "utf-16-le"
    if head.startswith(b"\xfe\xff"):
        return data.decode("utf-16-be", errors="replace"), "utf-16-be"
    if b"\x00" in head[:200]:  # UTF-16 without a BOM
        return data.decode("utf-16-le", errors="replace"), "utf-16-le"
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "cp1252"


class LogConnector:
    def __init__(self, dirs: list[Path], masker: Masker, since: dt.datetime | None = None):
        self.dirs = dirs
        self.masker = masker
        self.since = since  # only lines at or after this moment count as evidence
        self._files: list[Path] | None = None
        self._cache: dict[Path, list[_Line]] = {}
        self._cached_bytes = 0

    # ---- discovery ---------------------------------------------------------

    def discover(self) -> list[Path]:
        """Log files under the configured folders, newest first."""
        if self._files is None:
            found: dict[Path, float] = {}
            for root in self.dirs:
                if not root.is_dir():
                    continue
                for path in self._walk(root, 0):
                    try:
                        found.setdefault(path, path.stat().st_mtime)
                    except OSError:
                        continue
            self._files = sorted(found, key=lambda p: found[p], reverse=True)[:MAX_FILES]
        return self._files

    def _walk(self, d: Path, depth: int):
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for p in entries:
            try:
                if p.is_dir():
                    if depth < MAX_DEPTH and not p.is_symlink():
                        yield from self._walk(p, depth + 1)
                elif p.is_file() and p.suffix.lower() in LOG_SUFFIXES and (
                    LOG_NAME_HINT.search(p.name) or LOG_NAME_HINT.search(p.parent.name)
                ):
                    yield p
            except OSError:
                continue

    def display_path(self, path: Path) -> str:
        return self.masker.mask_path(str(path))

    # ---- reading -----------------------------------------------------------

    def _read(self, path: Path) -> list[_Line]:
        if path in self._cache:
            return self._cache[path]
        try:
            size = path.stat().st_size
            mtime = dt.datetime.fromtimestamp(path.stat().st_mtime)
            with open(path, "rb") as f:
                head = f.read(4)
                start = max(0, size - TAIL_BYTES)
                start -= start % 2  # stay aligned for UTF-16
                skipped_newlines = 0
                if start:
                    f.seek(0)
                    remaining = start
                    while remaining > 0:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        skipped_newlines += chunk.count(b"\n")
                        remaining -= len(chunk)
                f.seek(start)
                data = f.read()
        except OSError:
            return []
        text, _ = _decode(data, head)
        raw_lines = text.splitlines()
        first_no = skipped_newlines + 1
        if start and raw_lines:  # the first line is cut in half
            raw_lines = raw_lines[1:]
            first_no += 1
        lines: list[_Line] = []
        current: dt.datetime | None = None
        for i, raw in enumerate(raw_lines):
            ts = parse_timestamp(raw)
            if ts is not None:
                current = ts
            lines.append(_Line(first_no + i, raw, current, ts is not None))
        if lines and all(line.ts is None for line in lines):  # no timestamps at all: use the file time
            for line in lines:
                line.ts = mtime
        if self._cached_bytes + len(data) <= CACHE_BUDGET:
            self._cache[path] = lines
            self._cached_bytes += len(data)
        return lines

    # ---- searching ---------------------------------------------------------

    def find(self, pattern: str, limit: int = 5, context: int = 0,
             since: dt.datetime | None = None) -> LogSearch:
        """Most recent matches first, masked. Lines older than the window are counted, not returned."""
        rx = re.compile(pattern, re.IGNORECASE)
        since = since if since is not None else self.since
        hits: list[LogHit] = []
        older = searched = too_old = 0
        for path in self.discover():
            if since is not None:
                try:
                    if dt.datetime.fromtimestamp(path.stat().st_mtime) < since:
                        too_old += 1  # not written to inside the window
                        continue
                except OSError:
                    continue
            lines = self._read(path)
            searched += 1
            file_hits: list[LogHit] = []
            for idx, line in enumerate(lines):
                if not rx.search(line.text):
                    continue
                if since is not None and line.ts is not None and line.ts < since:
                    older += 1
                    continue
                following: list[str] = []
                for nxt in lines[idx + 1: idx + 1 + context]:
                    if nxt.starts_entry:
                        break
                    if nxt.text.strip():
                        following.append(self._mask(nxt.text))
                file_hits.append(LogHit(
                    self.display_path(path), line.no, self._mask(line.text),
                    line.ts.isoformat(sep=" ", timespec="seconds") if line.ts else None, following))
            hits.extend(reversed(file_hits))
            if len(hits) >= limit:
                break
        return LogSearch(hits[:limit], older, searched, too_old)

    def search(self, pattern: str, limit: int = 5, context: int = 0) -> list[LogHit]:
        return self.find(pattern, limit, context).hits

    def recent_errors(self, limit: int = 10, context: int = 0) -> list[LogHit]:
        return self.find(ERROR_LINE.pattern, limit, context).hits

    def _mask(self, text: str) -> str:
        return (self.masker.mask_text(text.strip()) or "")[:MAX_LINE]
