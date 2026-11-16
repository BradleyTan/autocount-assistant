"""System version vs database version compatibility check (read-only)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from ..config import Settings
from .plugins import PluginConnector
from .sqlserver import SqlServerConnector, SqlUnavailable

VERSION_RX = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?")
_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


def load_compat(path: Path | None = None) -> dict[str, str]:
    text = path.read_text() if path else resources.files("autocount_assist").joinpath("compat.yaml").read_text()
    return {str(k): str(v) for k, v in (yaml.safe_load(text) or {}).get("compatibility", {}).items()}


def vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


@dataclass
class VersionReport:
    app_version: str | None
    app_source: str
    db_version: str | None
    db_source: str
    expected_db: str | None
    status: str  # "ok" | "mismatch" | "unknown"
    direction: str = ""  # "db_older" | "db_newer"
    notes: list[str] = field(default_factory=list)


class VersionConnector:
    def __init__(self, settings: Settings, plugins: PluginConnector, sql: SqlServerConnector | None,
                 compat: dict[str, str] | None = None):
        self.settings = settings
        self.plugins = plugins
        self.sql = sql
        self.compat = compat if compat is not None else load_compat()

    def app_version(self) -> tuple[str | None, str]:
        if self.settings.app_version:
            return self.settings.app_version, "config: app_version"
        b = self.plugins.find(self.settings.app_exes)
        if b and (b.product_version or b.file_version):
            return (b.product_version or b.file_version), f"{b.path} (file version)"
        return None, f"none of {self.settings.app_exes} found under {self.settings.install_path}"

    def db_version(self) -> tuple[str | None, str]:
        if self.sql is None:
            return None, "no database selected"
        try:
            if self.settings.db_version_query:
                rows = self.sql.query(self.settings.db_version_query, mask=False)
                for r in rows:
                    for v in r.values():
                        m = VERSION_RX.search(str(v))
                        if m:
                            return m.group(0), "config: db_version_query"
                return None, "db_version_query returned no version"
            # Auto-discovery: any table with 'version' in its name and a version-like column.
            cols = self.sql.columns()
            for table in sorted(cols):
                if "version" not in table:
                    continue
                for col in sorted(c for c in cols[table] if "version" in c or c in ("value", "regvalue")):
                    if not (_IDENT.match(table) and _IDENT.match(col)):
                        continue
                    rows = self.sql.query(f"SELECT TOP 5 [{col}] AS v FROM [{table}]", mask=False)
                    for r in rows:
                        m = VERSION_RX.search(str(r.get("v")))
                        if m:
                            return m.group(0), f"{table}.{col} (auto-discovered; confirm with db_version_query)"
            return None, "no version table found; set db_version_query in the config"
        except SqlUnavailable as e:
            return None, str(e)

    def expected_db_for(self, app: str) -> str | None:
        if app in self.compat:
            return self.compat[app]
        short = ".".join(app.split(".")[:3])
        return self.compat.get(short)

    def check(self) -> VersionReport:
        app, app_src = self.app_version()
        db, db_src = self.db_version()
        expected = self.expected_db_for(app) if app else None
        rep = VersionReport(app, app_src, db, db_src, expected, "unknown")
        if not app:
            rep.notes.append("System version unknown")
        if not db:
            rep.notes.append("Database version unknown")
        if app and not expected:
            rep.notes.append(f"No compatibility entry for system version {app} in compat.yaml")
        if db and expected:
            if vtuple(db) == vtuple(expected):
                rep.status = "ok"
            else:
                rep.status = "mismatch"
                rep.direction = "db_older" if vtuple(db) < vtuple(expected) else "db_newer"
        return rep
