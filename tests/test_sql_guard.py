import pytest

from autocount_assist.sql_guard import ReadOnlyViolation, check_read_only

OK = [
    "SELECT TOP 5 * FROM StockDTL",
    "  select name from sys.databases;  ",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    "SELECT 'DELETE FROM Debtor' AS txt",  # keyword inside a string literal
    "SELECT 1 -- DROP TABLE x",
]
BAD = [
    "DELETE FROM Debtor",
    "UPDATE Item SET Qty = 0",
    "INSERT INTO Item VALUES (1)",
    "DROP TABLE Item",
    "SELECT * INTO backup_item FROM Item",
    "SELECT 1; DELETE FROM Item",
    "EXEC sp_who",
    "SELECT * FROM OPENROWSET('SQLNCLI','x','select 1')",
    "TRUNCATE TABLE GLDTL",
    "WITH x AS (SELECT 1 a) DELETE FROM Item",
    "SELECT 1 /* unterminated",
    "",
    "ALTER LOGIN sa ENABLE",
    "SELECT xp_cmdshell('dir')",
]


@pytest.mark.parametrize("sql", OK)
def test_allows_selects(sql):
    assert check_read_only(sql) == sql


@pytest.mark.parametrize("sql", BAD)
def test_rejects_writes(sql):
    with pytest.raises(ReadOnlyViolation):
        check_read_only(sql)
