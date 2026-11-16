"""Reject anything that is not a single read-only SELECT before it reaches SQL Server.

This is the first line of defence; the db_datareader-only login is the second.

The statement is scanned once, left to right, the way SQL Server reads it: string
literals, [bracketed] and "quoted" identifiers and comments are skipped as opaque
blocks, and only the remaining code is checked. Scanning in one pass matters: a
filter that strips comments first and strings second can be fooled by a comment
marker inside a string (``SELECT '--'; DROP TABLE x --'``).
"""

from __future__ import annotations

import re

__all__ = ["ReadOnlyViolation", "check_read_only", "is_read_only"]


class ReadOnlyViolation(Exception):
    pass


# Any of these keywords, anywhere in the code part of the statement, is refused.
# T-SQL does not need ";" between statements (SELECT 1 DELETE FROM t is two
# statements), so the keyword list is what stops a second statement.
FORBIDDEN_KEYWORDS = frozenset({
    # data and schema changes
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "ALTER", "CREATE", "INTO",
    "UPDATETEXT", "WRITETEXT", "READTEXT", "BULK",
    # running other code
    "EXEC", "EXECUTE", "CALL", "OPENROWSET", "OPENQUERY", "OPENDATASOURCE", "OPENXML",
    # permissions and server control
    "GRANT", "REVOKE", "DENY", "BACKUP", "RESTORE", "DBCC", "SHUTDOWN", "KILL", "RECONFIGURE",
    "CHECKPOINT", "WAITFOR",
    # session, batch and transaction control
    "USE", "SET", "DECLARE", "BEGIN", "COMMIT", "ROLLBACK", "SAVE", "GO", "GOTO", "WHILE",
    "IF", "RETURN", "THROW", "RAISERROR", "PRINT", "REVERT", "SETUSER", "DISABLE", "ENABLE",
})
# System and extended procedures (sp_executesql, xp_cmdshell, ...). To read a real
# column with such a name, write it in brackets: [sp_code].
_FORBIDDEN_PREFIX = re.compile(r"^(sp|xp)_", re.IGNORECASE)

_WORD_START = re.compile(r"[A-Za-z_@#]")
_WORD = re.compile(r"[A-Za-z_@#][\w@#$]*")


def _tokens(sql: str) -> list[str]:
    """Code tokens only: words (upper-cased) and single punctuation characters.

    Literals, delimited identifiers and comments are skipped. Raises
    ReadOnlyViolation when one of them is not closed.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch.isspace():
            i += 1
        elif ch == "-" and nxt == "-":  # line comment
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
        elif ch == "/" and nxt == "*":  # block comment; T-SQL nests them
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise ReadOnlyViolation("Unclosed /* comment")
        elif ch in "'\"":  # string literal or "quoted identifier"; doubled quote escapes
            i = _skip_delimited(sql, i, ch, "string" if ch == "'" else "quoted identifier")
        elif ch == "[":  # [bracketed identifier]; ]] escapes
            i = _skip_delimited(sql, i, "]", "[identifier]")
        elif _WORD_START.match(ch):
            m = _WORD.match(sql, i)
            assert m is not None
            out.append(m.group(0).upper())
            i = m.end()
        else:
            out.append(ch)
            i += 1
    return out


def _skip_delimited(sql: str, start: int, closer: str, what: str) -> int:
    i, n = start + 1, len(sql)
    while i < n:
        if sql[i] == closer:
            if i + 1 < n and sql[i + 1] == closer:  # escaped delimiter
                i += 2
                continue
            return i + 1
        i += 1
    raise ReadOnlyViolation(f"Unclosed {what}")


def check_read_only(sql: str) -> str:
    """Return the statement if it is a single SELECT/WITH query, else raise ReadOnlyViolation."""
    if not sql or not sql.strip():
        raise ReadOnlyViolation("Empty statement")
    if "\x00" in sql:
        raise ReadOnlyViolation("Statement contains a NUL character")
    tokens = _tokens(sql)
    while tokens and tokens[-1] == ";":  # trailing semicolons are harmless
        tokens.pop()
    if not tokens:
        raise ReadOnlyViolation("Empty statement")
    if ";" in tokens:
        raise ReadOnlyViolation("Only one statement is allowed")
    if tokens[0] not in ("SELECT", "WITH"):
        raise ReadOnlyViolation(f"Only SELECT queries are allowed (got '{tokens[0]}')")
    for i, tok in enumerate(tokens):
        if tok in FORBIDDEN_KEYWORDS or _FORBIDDEN_PREFIX.match(tok):
            raise ReadOnlyViolation(
                f"Forbidden keyword '{tok}' in read-only query "
                "(if it is a column name, write it in [brackets])")
        if tok == "NEXT" and tokens[i + 1:i + 2] == ["VALUE"]:  # NEXT VALUE FOR advances a sequence
            raise ReadOnlyViolation("NEXT VALUE FOR is not allowed in a read-only query")
    return sql


def is_read_only(sql: str) -> bool:
    try:
        check_read_only(sql)
    except ReadOnlyViolation:
        return False
    return True
