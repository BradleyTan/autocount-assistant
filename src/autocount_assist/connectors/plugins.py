"""List installed DLLs / EXEs with their file versions (read-only)."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

BINARY_SUFFIXES = {".dll", ".exe"}
MAX_DEPTH = 3
_VERSION_KEYS = ("FileVersion", "ProductVersion")


@dataclass
class BinaryInfo:
    path: str
    name: str
    file_version: str | None
    product_version: str | None

    @property
    def is_autocount_core(self) -> bool:
        return self.name.lower().startswith("autocount")


def read_versions(path: Path) -> tuple[str | None, str | None]:
    if sys.platform == "win32":
        v = _win_versions(path)
        if v != (None, None):
            return v
    return _scan_versions(path)


def _win_versions(path: Path) -> tuple[str | None, str | None]:
    try:
        import ctypes
        from ctypes import wintypes

        ver = ctypes.WinDLL("version", use_last_error=True)
        size = ver.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None, None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
            return None, None
        lp = ctypes.c_void_p()
        n = wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(lp), ctypes.byref(n)) or not n.value:
            return None, None
        lang, cp = ctypes.cast(lp, ctypes.POINTER(wintypes.WORD * 2)).contents
        out = []
        for key in _VERSION_KEYS:
            q = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\{key}"
            if ver.VerQueryValueW(buf, q, ctypes.byref(lp), ctypes.byref(n)) and n.value:
                out.append(ctypes.wstring_at(lp.value, n.value - 1).strip() or None)
            else:
                out.append(None)
        return out[0], out[1]
    except (OSError, AttributeError, ValueError):
        return None, None


def _scan_versions(path: Path) -> tuple[str | None, str | None]:
    """Portable fallback: find the UTF-16 VS_VERSIONINFO strings in the binary."""
    try:
        data = path.read_bytes()
    except OSError:
        return None, None
    out = []
    for key in _VERSION_KEYS:
        marker = key.encode("utf-16-le") + b"\x00\x00"
        i = data.rfind(marker)
        if i < 0:
            out.append(None)
            continue
        j = i + len(marker)
        while j < len(data) - 1 and data[j:j + 2] == b"\x00\x00":  # padding
            j += 2
        end = data.find(b"\x00\x00", j)
        if (end - j) % 2:
            end += 1
        raw = data[j:end].decode("utf-16-le", errors="ignore")
        m = re.match(r"[\d.]+", raw.strip())
        out.append(m.group(0) if m else None)
    return out[0], out[1]


class PluginConnector:
    def __init__(self, dirs: list[Path]):
        self.dirs = dirs

    def list_binaries(self) -> list[BinaryInfo]:
        out: list[BinaryInfo] = []
        seen: set[Path] = set()
        for root in self.dirs:
            if root.is_dir():
                for p in self._walk(root, 0):
                    if p in seen:
                        continue
                    seen.add(p)
                    fv, pv = read_versions(p)
                    out.append(BinaryInfo(str(p), p.name, fv, pv))
        return sorted(out, key=lambda b: b.name.lower())

    def _walk(self, d: Path, depth: int):
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for p in entries:
            if p.is_dir() and depth < MAX_DEPTH:
                yield from self._walk(p, depth + 1)
            elif p.is_file() and p.suffix.lower() in BINARY_SUFFIXES:
                yield p

    def find(self, names: list[str]) -> BinaryInfo | None:
        wanted = [n.lower() for n in names]
        for b in self.list_binaries():
            if b.name.lower() in wanted:
                return b
        return None

    def core_version_mismatches(self, app_version: str) -> list[BinaryInfo]:
        """AutoCount*.dll files whose major.minor.build differs from the app's (a partial upgrade)."""
        want = _norm(app_version, 3)
        return [b for b in self.list_binaries()
                if b.is_autocount_core and b.file_version and _norm(b.file_version, 3) != want]


def _norm(version: str, parts: int) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", version)][:parts]
    return tuple(nums + [0] * (parts - len(nums)))
