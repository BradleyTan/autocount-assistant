"""Read AutoCount .config / .xml / .ini / .json files as masked key/value pairs (read-only)."""

from __future__ import annotations

import configparser
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from ..masking import Masker

CONFIG_SUFFIXES = {".config", ".xml", ".ini", ".json"}
MAX_DEPTH = 2
MAX_SIZE = 1024 * 1024
SECRET_KEY = re.compile(r"(?i)(password|pwd|secret|token|apikey|api_key|connectionstring|clientsecret)")


@dataclass
class ConfigEntry:
    file: str
    key: str
    value: str  # masked


class ConfigConnector:
    def __init__(self, dirs: list[Path], masker: Masker):
        self.dirs = dirs
        self.masker = masker
        self._entries: list[ConfigEntry] | None = None

    def files(self) -> list[Path]:
        out: list[Path] = []
        for root in self.dirs:
            if root.is_dir():
                for p in self._walk(root, 0):
                    out.append(p)
        return out

    def _walk(self, d: Path, depth: int):
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for p in entries:
            if p.is_dir() and depth < MAX_DEPTH:
                yield from self._walk(p, depth + 1)
            elif p.is_file() and p.suffix.lower() in CONFIG_SUFFIXES:
                try:
                    if p.stat().st_size <= MAX_SIZE:
                        yield p
                except OSError:
                    pass

    def entries(self) -> list[ConfigEntry]:
        if self._entries is None:
            self._entries = []
            for path in self.files():
                for key, value in self._parse(path):
                    self._entries.append(ConfigEntry(str(path), key, self._mask(key, value)))
        return self._entries

    def _mask(self, key: str, value: str) -> str:
        if SECRET_KEY.search(key):
            return self.masker.mask_text(value) if "=" in value and ";" in value else "<SECRET>"
        return self.masker.mask_text(value) or ""

    def find(self, key_pattern: str, value_pattern: str | None = None) -> list[ConfigEntry]:
        krx = re.compile(key_pattern, re.IGNORECASE)
        vrx = re.compile(value_pattern, re.IGNORECASE) if value_pattern else None
        return [e for e in self.entries() if krx.search(e.key) and (vrx is None or vrx.search(e.value))]

    @staticmethod
    def _parse(path: Path):
        suffix = path.suffix.lower()
        try:
            if suffix in (".config", ".xml"):
                yield from _parse_xml(path)
            elif suffix == ".ini":
                cp = configparser.ConfigParser(interpolation=None, strict=False)
                cp.read(path, encoding="utf-8")
                for section in cp.sections():
                    for k, v in cp.items(section):
                        yield f"{section}.{k}", v
            elif suffix == ".json":
                yield from _flatten("", json.loads(path.read_text(encoding="utf-8-sig")))
        except (ET.ParseError, configparser.Error, json.JSONDecodeError, UnicodeDecodeError, OSError):
            return


def _parse_xml(path: Path):
    root = ET.parse(path).getroot()

    def walk(el, prefix):
        tag = el.tag.split("}")[-1]
        name = f"{prefix}.{tag}" if prefix else tag
        attrs = dict(el.attrib)
        # .NET style <add key="x" value="y"/> and <add name="x" connectionString="y"/>
        if "key" in attrs and "value" in attrs:
            yield f"{name}.{attrs['key']}", attrs["value"]
        elif "name" in attrs and "connectionString" in attrs:
            yield f"{name}.{attrs['name']}.connectionString", attrs["connectionString"]
        else:
            for k, v in attrs.items():
                yield f"{name}@{k}", v
        text = (el.text or "").strip()
        if text and len(el) == 0:
            yield name, text
        for child in el:
            yield from walk(child, name)

    yield from walk(root, "")


def _flatten(prefix, obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(f"{prefix}.{k}" if prefix else str(k), v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _flatten(f"{prefix}[{i}]", v)
    else:
        yield prefix, "" if obj is None else str(obj)
