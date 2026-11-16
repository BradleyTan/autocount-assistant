"""A tiny DB-API stand-in used by tests and `autocount-assist demo`.

It answers queries by regex, so no real SQL Server or customer data is needed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

READ_ONLY_LOGIN: dict[str, Any] = {
    "login_name": "ac_readonly", "database_name": "AED_DEMO", "is_sysadmin": 0, "is_db_owner": 0,
    "is_datawriter": 0, "is_ddladmin": 0, "can_insert": 0, "can_update": 0, "can_delete": 0,
    "can_alter": 0, "can_execute": 0,
}
DEFAULT_SERVER: dict[str, Any] = {
    "product_version": "16.0.1000.6", "product_level": "RTM", "edition": "Express Edition (64-bit)",
    "engine_edition": 4, "database_name": "AED_DEMO", "status": "ONLINE", "recovery_model": "SIMPLE",
    "collation": "SQL_Latin1_General_CP1_CI_AS", "compatibility_level": 160,
}
DEFAULT_FILES: list[dict[str, Any]] = [
    {"type_desc": "ROWS", "size_mb": 850, "physical_name": r"C:\SQLData\AED_DEMO.mdf"},
    {"type_desc": "LOG", "size_mb": 120, "physical_name": r"C:\SQLData\AED_DEMO_log.ldf"},
]

Schema = Mapping[str, "Sequence[str] | Mapping[str, str]"]


class FakeCursor:
    def __init__(self, conn: FakeConnection):
        self.conn = conn
        self.description: list[tuple[str]] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.conn.executed.append(sql)
        rows = self.conn.answer(sql, params)
        cols = list(rows[0].keys()) if rows else ["_"]
        self.description = [(c,) for c in cols]
        self._rows = [tuple(r.get(c) for c in cols) for r in rows]

    def fetchmany(self, n: int) -> list[tuple[Any, ...]]:
        return self._rows[:n]

    def close(self) -> None:
        pass


class FakeConnection:
    """schema: {Table: [Column, ...]} or {Table: {Column: sql_type}}."""

    def __init__(self, schema: Schema | None = None,
                 responses: list[tuple[str, list[dict[str, Any]]]] | None = None,
                 databases: list[str] | None = None,
                 login: dict[str, Any] | None = None,
                 server: dict[str, Any] | None = None,
                 files: list[dict[str, Any]] | None = None,
                 row_counts: dict[str, int] | None = None):
        self.schema: dict[str, dict[str, str]] = {
            table: (dict(cols) if isinstance(cols, Mapping) else {c: "nvarchar" for c in cols})
            for table, cols in (schema or {}).items()}
        self.responses = [(re.compile(p, re.IGNORECASE | re.DOTALL), rows) for p, rows in (responses or [])]
        self.databases = databases or ["master", "AED_DEMO", "AED_TRAINING"]
        self.login = READ_ONLY_LOGIN | (login or {})
        self.server = DEFAULT_SERVER | (server or {})
        self.files = files if files is not None else DEFAULT_FILES
        self.row_counts = row_counts or {}
        self.executed: list[str] = []
        self.timeout = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        pass

    @property
    def queries(self) -> list[str]:
        """Statements sent, without the session setup."""
        return [s for s in self.executed if not s.startswith("SET NOCOUNT ON")]

    def answer(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        if sql.startswith("SET NOCOUNT ON"):
            return []
        if "SUSER_SNAME()" in sql:
            return [dict(self.login)]
        if "SERVERPROPERTY" in sql:
            return [dict(self.server)]
        if "sys.database_files" in sql:
            if "physical_name" in sql:
                return [{"type_desc": f["type_desc"], "physical_name": f.get("physical_name", "")}
                        for f in self.files]
            return [{"type_desc": f["type_desc"], "size_mb": f["size_mb"]} for f in self.files]
        if "sys.partitions" in sql:
            counts = self.row_counts or {t: 0 for t in self.schema}
            ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            return [{"table_name": t, "row_count": n} for t, n in ranked]
        if "sys.databases" in sql:
            return [{"name": d, "state_desc": "ONLINE", "create_date": "2024-01-01"} for d in self.databases]
        if "INFORMATION_SCHEMA.TABLES" in sql and params:
            rx = re.compile("^" + re.escape(params[0]).replace("%", ".*") + "$", re.IGNORECASE)
            return [{"TABLE_NAME": t} for t in self.schema if rx.match(t)]
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return [{"TABLE_NAME": t, "COLUMN_NAME": c, "DATA_TYPE": dtype,
                     "CHARACTER_MAXIMUM_LENGTH": 50, "IS_NULLABLE": "YES"}
                    for t, cols in self.schema.items() for c, dtype in cols.items()]
        if "sys.foreign_keys" in sql:
            return []
        for rx, rows in self.responses:
            if rx.search(sql):
                return rows
        return []


def fake_connect_factory(conn: FakeConnection):
    return lambda _conn_str: conn
