import pytest

from autocount_assist.config import Settings
from autocount_assist.connectors.plugins import PluginConnector
from autocount_assist.connectors.sqlserver import SqlServerConnector, build_connection_string
from autocount_assist.connectors.version import VersionConnector
from autocount_assist.fakedb import FakeConnection, fake_connect_factory
from autocount_assist.masking import Masker
from autocount_assist.sql_guard import ReadOnlyViolation


def make(conn, **settings):
    s = Settings(**settings)
    return SqlServerConnector(s, Masker(b"s"), "AED_DEMO", connect=fake_connect_factory(conn)), s


def test_connection_string_uses_env_and_read_only(monkeypatch):
    monkeypatch.setenv("AC_SQL_USER", "ac_readonly")
    monkeypatch.setenv("AC_SQL_PASSWORD", "pw!")
    cs = build_connection_string(Settings(), "AED_X")
    assert "ApplicationIntent=ReadOnly" in cs and "UID=ac_readonly" in cs and "DATABASE=AED_X" in cs
    monkeypatch.delenv("AC_SQL_USER")
    cs = build_connection_string(Settings(), None)
    assert "Trusted_Connection=yes" in cs and "PWD" not in cs


def test_repr_has_no_credentials(monkeypatch):
    monkeypatch.setenv("AC_SQL_PASSWORD", "topsecret")
    sql, _ = make(FakeConnection())
    assert "topsecret" not in repr(sql)


def test_query_is_guarded_and_never_sent():
    conn = FakeConnection()
    sql, _ = make(conn)
    with pytest.raises(ReadOnlyViolation):
        sql.query("DELETE FROM Debtor")
    assert conn.executed == []


def test_rows_are_masked():
    conn = FakeConnection(responses=[(r"FROM Debtor", [{"AccNo": "300-A001", "CompanyName": "Kedai Ah Kow"}])])
    sql, _ = make(conn)
    rows = sql.query("SELECT AccNo, CompanyName FROM Debtor")
    assert rows[0]["AccNo"] == "300-A001" and "Ah Kow" not in rows[0]["CompanyName"]


def test_list_databases_flags_account_books():
    sql, _ = make(FakeConnection(databases=["master", "AED_ACME", "ReportServer"]))
    dbs = {d["name"]: d["looks_like_account_book"] for d in sql.list_databases()}
    assert dbs == {"AED_ACME": True, "ReportServer": False}


def test_missing_objects():
    sql, _ = make(FakeConnection(schema={"StockDTL": ["ItemCode", "Qty"]}))
    assert sql.missing_objects({"StockDTL": ["ItemCode", "Location"], "ItemUOM": ["Rate"]}) == \
        ["StockDTL.Location", "ItemUOM"]


@pytest.mark.parametrize("db_version, status, direction", [
    ("2.2.97", "ok", ""), ("2.2.95", "mismatch", "db_older"), ("2.2.99", "mismatch", "db_newer")])
def test_version_compatibility(tmp_path, db_version, status, direction):
    conn = FakeConnection(schema={"DBVersion": ["Version"]},
                          responses=[(r"FROM \[dbversion\]", [{"v": db_version}])])
    sql, s = make(conn, app_version="2.2.32.45")
    rep = VersionConnector(s, PluginConnector([tmp_path]), sql).check()
    assert (rep.status, rep.direction) == (status, direction)
    assert rep.expected_db == "2.2.97"


def test_version_unknown_without_db(tmp_path):
    s = Settings(app_version="2.2.32.45")
    rep = VersionConnector(s, PluginConnector([tmp_path]), None).check()
    assert rep.status == "unknown" and "Database version unknown" in rep.notes
