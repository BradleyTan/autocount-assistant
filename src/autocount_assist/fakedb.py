"""A tiny DB-API stand-in used by tests and `autocount-assist demo`.

It answers queries by regex, so no real SQL Server or customer data is needed.
"""

from __future__ import annotations

import re
from typing import Any


class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self.conn = conn
        self.description = None
        self._rows: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()):
        self.conn.executed.append(sql)
        rows = self.conn.answer(sql, params)
        cols = list(rows[0].keys()) if rows else ["_"]
        self.description = [(c,) for c in cols]
        self._rows = [tuple(r.get(c) for c in cols) for r in rows]

    def fetchmany(self, n: int):
        return self._rows[:n]

    def close(self):
        pass


class FakeConnection:
    def __init__(self, schema: dict[str, list[str]] | None = None,
                 responses: list[tuple[str, list[dict[str, Any]]]] | None = None,
                 databases: list[str] | None = None):
        self.schema = schema or {}
        self.responses = [(re.compile(p, re.IGNORECASE | re.DOTALL), rows) for p, rows in (responses or [])]
        self.databases = databases or ["master", "AED_DEMO", "AED_TRAINING"]
        self.executed: list[str] = []

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass

    def answer(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        if "sys.databases" in sql:
            return [{"name": d, "state_desc": "ONLINE", "create_date": "2024-01-01"} for d in self.databases]
        if "INFORMATION_SCHEMA.TABLES" in sql and params:
            rx = re.compile("^" + re.escape(params[0]).replace("%", ".*") + "$", re.IGNORECASE)
            return [{"TABLE_NAME": t} for t in self.schema if rx.match(t)]
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return [{"TABLE_NAME": t, "COLUMN_NAME": c, "DATA_TYPE": "nvarchar",
                     "CHARACTER_MAXIMUM_LENGTH": 50, "IS_NULLABLE": "YES"}
                    for t, cols in self.schema.items() for c in cols]
        if "sys.foreign_keys" in sql:
            return []
        for rx, rows in self.responses:
            if rx.search(sql):
                return rows
        return []


def fake_connect_factory(conn: FakeConnection):
    return lambda _conn_str: conn
