"""Read-only SQL Server connector for AutoCount account book databases.

Safety layers:
1. Every statement passes sql_guard.check_read_only() before it is sent.
2. The connection is opened with ApplicationIntent=ReadOnly and pyodbc readonly=True.
3. The login itself should be db_datareader only (see sql/create_readonly_login.sql).
Every returned row is masked.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from ..config import Settings
from ..masking import Masker
from ..sql_guard import check_read_only

SYSTEM_DBS = {"master", "tempdb", "model", "msdb"}
_IDENT = re.compile(r"^[A-Za-z0-9_\- .]+$")


class SqlUnavailable(Exception):
    """No database connection could be made; checks that need SQL report missing evidence."""


def build_connection_string(settings: Settings, database: str | None) -> str:
    parts = [
        f"DRIVER={{{settings.odbc_driver}}}",
        f"SERVER={settings.sql_server}",
        "ApplicationIntent=ReadOnly",
        "Encrypt=yes",
        "TrustServerCertificate=yes",
    ]
    if database:
        parts.append(f"DATABASE={database}")
    user = settings.sql_user()
    if user:
        parts.append(f"UID={user}")
        parts.append(f"PWD={settings.sql_password() or ''}")
    else:
        parts.append("Trusted_Connection=yes")
    return ";".join(parts)


def _pyodbc_connect(conn_str: str):
    try:
        import pyodbc
    except ImportError as e:
        raise SqlUnavailable("pyodbc is not installed (pip install pyodbc)") from e
    try:
        return pyodbc.connect(conn_str, readonly=True, autocommit=True, timeout=10)
    except pyodbc.Error as e:
        # Never echo the connection string: it may contain the password.
        raise SqlUnavailable(f"Could not connect to SQL Server: {e.args[-1] if e.args else e}") from None


class SqlServerConnector:
    def __init__(self, settings: Settings, masker: Masker, database: str | None = None,
                 connect: Callable[[str], Any] | None = None):
        self.settings = settings
        self.masker = masker
        self.database = database
        self._connect = connect or _pyodbc_connect
        self._conn = None
        self._columns_cache: dict[str, set[str]] | None = None

    def __repr__(self) -> str:  # never include credentials
        return f"SqlServerConnector(server={self.settings.sql_server!r}, database={self.database!r})"

    def _connection(self):
        if self._conn is None:
            self._conn = self._connect(build_connection_string(self.settings, self.database))
        return self._conn

    def query(self, sql: str, params: tuple = (), mask: bool = True, limit: int = 200) -> list[dict[str, Any]]:
        check_read_only(sql)
        cur = self._connection().cursor()
        try:
            cur.execute(sql, params) if params else cur.execute(sql)
            cols = [d[0] for d in cur.description or []]
            rows = [dict(zip(cols, r)) for r in cur.fetchmany(limit)]
        finally:
            cur.close()
        return [self.masker.mask_row(r) for r in rows] if mask else rows

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- discovery -------------------------------------------------------

    def list_databases(self) -> list[dict[str, Any]]:
        """User databases, with a flag for those that look like AutoCount account books."""
        rows = self.query(
            "SELECT name, state_desc, create_date FROM sys.databases ORDER BY name", mask=False)
        out = []
        for r in rows:
            if r["name"].lower() in SYSTEM_DBS:
                continue
            r["looks_like_account_book"] = r["name"].upper().startswith("AED_")
            out.append(r)
        return out

    def columns(self) -> dict[str, set[str]]:
        """{table_lower: {column_lower,...}} for the current database."""
        if self._columns_cache is None:
            rows = self.query(
                "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS", mask=False, limit=100000)
            cache: dict[str, set[str]] = {}
            for r in rows:
                cache.setdefault(r["TABLE_NAME"].lower(), set()).add(r["COLUMN_NAME"].lower())
            self._columns_cache = cache
        return self._columns_cache

    def missing_objects(self, requires: dict[str, list[str]]) -> list[str]:
        """Return 'Table' or 'Table.Column' names that don't exist in this database."""
        cols = self.columns()
        missing = []
        for table, needed in (requires or {}).items():
            have = cols.get(table.lower())
            if have is None:
                missing.append(table)
                continue
            missing.extend(f"{table}.{c}" for c in needed if c.lower() not in have)
        return missing

    def schema(self) -> dict[str, Any]:
        """Tables, columns and foreign keys for the knowledge base."""
        columns = self.query(
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE "
            "FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_NAME, ORDINAL_POSITION",
            mask=False, limit=200000)
        fks = self.query(
            "SELECT fk.name AS constraint_name, tp.name AS parent_table, cp.name AS parent_column, "
            "tr.name AS ref_table, cr.name AS ref_column "
            "FROM sys.foreign_keys fk "
            "JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
            "JOIN sys.tables tp ON tp.object_id = fkc.parent_object_id "
            "JOIN sys.columns cp ON cp.object_id = fkc.parent_object_id AND cp.column_id = fkc.parent_column_id "
            "JOIN sys.tables tr ON tr.object_id = fkc.referenced_object_id "
            "JOIN sys.columns cr ON cr.object_id = fkc.referenced_object_id AND cr.column_id = fkc.referenced_column_id",
            mask=False, limit=100000)
        return {"columns": columns, "foreign_keys": fks}

    def tables_like(self, pattern: str) -> list[str]:
        if not _IDENT.match(pattern.replace("%", "")):
            raise ValueError("Invalid table pattern")
        rows = self.query(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME LIKE ? ORDER BY TABLE_NAME",
            (pattern,), mask=False)
        return [r["TABLE_NAME"] for r in rows]
