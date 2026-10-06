"""Find and search AutoCount log / exception files (read-only)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..masking import Masker

LOG_SUFFIXES = {".log", ".txt"}
LOG_NAME_HINT = re.compile(r"(?i)(log|error|exception|trace|crash)")
ERROR_LINE = re.compile(r"(?i)\b(exception|error|failed|fatal)\b")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?")
MAX_DEPTH = 4
TAIL_BYTES = 2 * 1024 * 1024  # only the last 2 MB of each file is searched


@dataclass
class LogHit:
    file: str
    line_no: int
    line: str  # masked
    timestamp: str | None

    @property
    def ref(self) -> str:
        return f"{self.file}:{self.line_no}" if self.line_no > 0 else self.file


class LogConnector:
    def __init__(self, dirs: list[Path], masker: Masker):
        self.dirs = dirs
        self.masker = masker
        self._files: list[Path] | None = None

    def discover(self) -> list[Path]:
        if self._files is None:
            found: list[Path] = []
            for root in self.dirs:
                if not root.is_dir():
                    continue
                for path in self._walk(root, 0):
                    if path not in found:
                        found.append(path)
            self._files = sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)
        return self._files

    def _walk(self, d: Path, depth: int):
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for p in entries:
            if p.is_dir() and depth < MAX_DEPTH:
                yield from self._walk(p, depth + 1)
            elif p.is_file() and p.suffix.lower() in LOG_SUFFIXES and (
                LOG_NAME_HINT.search(p.name) or LOG_NAME_HINT.search(p.parent.name)
            ):
                yield p

    def _lines(self, path: Path):
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                start = max(0, size - TAIL_BYTES)
                f.seek(start)
                data = f.read()
        except OSError:
            return
        text = data.decode("utf-8", errors="replace")
        if "\x00" in text[:200]:  # UTF-16 logs
            text = data.decode("utf-16", errors="replace")
        first_line = 1 if start == 0 else None
        for i, line in enumerate(text.splitlines(), start=1):
            yield (i if first_line else -1), line

    def search(self, pattern: str, limit: int = 5) -> list[LogHit]:
        """Most recent matches first, masked."""
        rx = re.compile(pattern, re.IGNORECASE)
        hits: list[LogHit] = []
        for path in self.discover():
            file_hits = []
            for line_no, line in self._lines(path):
                if rx.search(line):
                    ts = TIMESTAMP.search(line)
                    file_hits.append(LogHit(str(path), line_no, self.masker.mask_text(line.strip())[:400],
                                            ts.group(0) if ts else None))
            hits.extend(reversed(file_hits))
            if len(hits) >= limit:
                break
        return hits[:limit]

    def recent_errors(self, limit: int = 10) -> list[LogHit]:
        return self.search(ERROR_LINE.pattern, limit)
