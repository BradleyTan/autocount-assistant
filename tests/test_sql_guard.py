import random

import pytest

from autocount_assist.sql_guard import ReadOnlyViolation, check_read_only, is_read_only

OK = [
    "SELECT TOP 5 * FROM StockDTL",
    "  select name from sys.databases;  ",
    "SELECT 1;;",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    "SELECT 'DELETE FROM Debtor' AS txt",  # keyword inside a string literal
    "SELECT N'it''s; DROP TABLE x' AS txt",  # escaped quote and ; inside a string
    "SELECT 1 -- DROP TABLE x",
    "SELECT 1 /* outer /* nested */ still a comment; DELETE */ AS a",
    "SELECT [Update], [sp_code], \"Delete\" FROM [Order Details]",  # delimited identifiers
    "SELECT [a]]b] FROM t",  # escaped bracket
    "SELECT * FROM t WITH (NOLOCK) ORDER BY a OFFSET 0 ROWS FETCH NEXT 5 ROWS ONLY",
    "SELECT CAST(SERVERPROPERTY('Edition') AS nvarchar(128)) AS e",
    "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT') AS can_insert",
    "SELECT a FROM t WHERE b = '--' AND c = '/*'",
]
BAD = [
    "DELETE FROM Debtor",
    "UPDATE Item SET Qty = 0",
    "INSERT INTO Item VALUES (1)",
    "DROP TABLE Item",
    "SELECT * INTO backup_item FROM Item",
    "SELECT 1; DELETE FROM Item",
    "SELECT 1 DELETE FROM Item",  # T-SQL needs no semicolon between statements
    "EXEC sp_who",
    "SELECT * FROM OPENROWSET('SQLNCLI','x','select 1')",
    "TRUNCATE TABLE GLDTL",
    "WITH x AS (SELECT 1 a) DELETE FROM Item",
    "SELECT 1 /* unterminated",
    "SELECT 'unterminated",
    "SELECT [unterminated",
    "",
    "   ",
    ";",
    "-- only a comment",
    "ALTER LOGIN sa ENABLE",
    "SELECT xp_cmdshell('dir')",
    "SELECT 1 EXEC sp_executesql N'DROP TABLE x'",
    "SELECT NEXT VALUE FOR dbo.Seq",
    "SELECT 1 WAITFOR DELAY '00:10:00'",
    "SELECT 1 BEGIN TRAN",
    "SET NOCOUNT ON",
    "USE master",
    "SELECT 1\x00; DROP TABLE x",
    # Bypasses of the old strip-then-scan guard: the comment marker or the quote is
    # inside a string or a delimited identifier, hiding the second statement.
    "SELECT '--'; DROP TABLE x --'",
    "SELECT [a'b] FROM t; DROP TABLE x; SELECT [c'd] FROM t",
    "SELECT \"a'b\" FROM t; DELETE FROM t; SELECT \"c'd\" FROM t",
    "SELECT '/*'; DROP TABLE x --*/'",
    "SELECT 1 /* a /* nested */ ; DROP TABLE x",
    "SELECT ']' FROM t; DROP TABLE x --[",
]


@pytest.mark.parametrize("sql", OK)
def test_allows_selects(sql):
    assert check_read_only(sql) == sql
    assert is_read_only(sql)


@pytest.mark.parametrize("sql", BAD)
def test_rejects_writes(sql):
    with pytest.raises(ReadOnlyViolation):
        check_read_only(sql)
    assert not is_read_only(sql)


def test_error_message_suggests_brackets_for_column_names():
    with pytest.raises(ReadOnlyViolation, match=r"\[brackets\]"):
        check_read_only("SELECT Enable FROM Users")


def test_fuzz_a_write_keyword_in_code_is_never_accepted():
    """Randomly wrap a DELETE in comments, strings and identifiers; whenever it is real
    code (not inside one of them) the guard must refuse."""
    rng = random.Random(20261116)
    wrappers = ["'{}'", "[{}]", '"{}"', "/* {} */", "-- {}\n"]
    noise = ["'--'", "'/*'", "[a'b]", '"x]y"', "/* ' */", "-- '\n", "N'q''q'", "[]]]", "'*/'"]
    for _ in range(3000):
        parts = ["SELECT 1"]
        for _ in range(rng.randint(0, 4)):
            parts.append(rng.choice(noise))
        hidden = rng.random() < 0.5
        payload = "DELETE FROM t"
        parts.append(rng.choice(wrappers).format(payload) if hidden else payload)
        for _ in range(rng.randint(0, 3)):
            parts.append(rng.choice(noise))
        sql = " , ".join(parts) if hidden else " ".join(parts)
        if hidden:
            assert is_read_only(sql), sql
        else:
            assert not is_read_only(sql), sql
