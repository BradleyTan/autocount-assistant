"""Reject anything that is not a single read-only SELECT before it reaches SQL Server.

This is the first line of defence; the db_datareader-only login is the second.
"""

from __future__ import annotations

import re


class ReadOnlyViolation(Exception):
    pass


_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE|DENY|"
    r"INTO|OPENROWSET|OPENQUERY|OPENDATASOURCE|OPENXML|BULK|BACKUP|RESTORE|DBCC|SHUTDOWN|KILL|"
    r"WAITFOR|USE|DECLARE|SET|RECONFIGURE|CHECKPOINT|READTEXT|WRITETEXT|UPDATETEXT|"
    r"SP_\w+|XP_\w+)\b",
    re.IGNORECASE,
)


def _strip(sql: str) -> str:
    """Remove comments and string literals so keywords inside them don't count."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"N?'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r'"(?:[^"]|"")*"', '""', sql)
    return sql


def check_read_only(sql: str) -> str:
    """Return the statement if it is a single SELECT/WITH query, else raise ReadOnlyViolation."""
    if not sql or not sql.strip():
        raise ReadOnlyViolation("Empty statement")
    body = _strip(sql).strip()
    if "/*" in body or "*/" in body:
        raise ReadOnlyViolation("Unbalanced comment")
    body = body.rstrip(";").strip()
    if ";" in body:
        raise ReadOnlyViolation("Only one statement is allowed")
    first = body.split(None, 1)[0].upper() if body else ""
    if first not in ("SELECT", "WITH"):
        raise ReadOnlyViolation(f"Only SELECT queries are allowed (got '{first}')")
    m = _FORBIDDEN.search(body)
    if m:
        raise ReadOnlyViolation(f"Forbidden keyword '{m.group(0).upper()}' in read-only query")
    return sql
