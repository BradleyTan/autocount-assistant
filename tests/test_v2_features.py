"""Tests for the schema-adaptive checks, the login gate, the new commands and the setup helpers."""

import datetime as dt
import json

import pytest
import yaml
from test_cli import setup

from autocount_assist import doctor, setup_login
from autocount_assist.cli import main, parse_since
from autocount_assist.config import ConfigError, Settings, load_settings
from autocount_assist.connectors import system
from autocount_assist.connectors.logs import LogConnector, parse_timestamp
from autocount_assist.connectors.sqlserver import (
    PrivilegedLogin,
    SqlServerConnector,
    SqlUnavailable,
    _odbc_value,
    build_connection_string,
    pick_driver,
    render_query,
)
from autocount_assist.fakedb import FakeConnection, fake_connect_factory
from autocount_assist.knowledge.store import KnowledgeStore
from autocount_assist.masking import Masker
from autocount_assist.playbook import load_playbooks, validate_playbook
from autocount_assist.recommender import render_ticket_note
from autocount_assist.sql_guard import check_read_only

WRITER = {"login_name": "sa", "is_sysadmin": 1, "can_update": 1}


def connector(conn, **settings):
    return SqlServerConnector(Settings(**settings), Masker(b"s"), "AED_DEMO", connect=fake_connect_factory(conn))


# ---- SQL connector ---------------------------------------------------------

def test_driver_auto_picks_newest():
    assert pick_driver("auto", ["SQL Server", "ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server"]) \
        == "ODBC Driver 18 for SQL Server"
    assert pick_driver("auto", ["SQL Server"]) == "SQL Server"
    assert pick_driver("auto", []) is None
    assert pick_driver("My Driver", ["x"]) == "My Driver"


def test_connection_string_values_cannot_inject(monkeypatch):
    monkeypatch.setenv("AC_SQL_USER", "ac_readonly")
    monkeypatch.setenv("AC_SQL_PASSWORD", "p;Trusted_Connection=yes")
    cs = build_connection_string(Settings(), "AED_X", driver="ODBC Driver 18 for SQL Server")
    assert "PWD={p;Trusted_Connection=yes}" in cs
    assert _odbc_value("a}b") == "{a}}b}" and _odbc_value("plain") == "plain"
    assert "Encrypt" not in build_connection_string(Settings(), None, driver="SQL Server")


def test_session_is_prepared_not_to_block_users():
    conn = FakeConnection()
    connector(conn, lock_timeout_ms=1234, query_timeout=7).query("SELECT 1 AS x")
    assert "SET LOCK_TIMEOUT 1234" in conn.executed[0] and "DEADLOCK_PRIORITY LOW" in conn.executed[0]
    assert conn.timeout == 7


def test_login_that_can_write_is_refused():
    conn = FakeConnection(login=WRITER, responses=[("FROM Debtor", [{"AccNo": "300-A001"}])])
    with pytest.raises(PrivilegedLogin) as e:
        connector(conn).query("SELECT AccNo FROM Debtor")
    assert "setup-login" in str(e.value) and "is_sysadmin" in str(e.value)
    assert not any("Debtor" in q for q in conn.queries)  # the query itself was never sent
    assert connector(conn, allow_privileged_login=True).query("SELECT AccNo FROM Debtor")


def test_resolve_objects_picks_the_names_this_database_uses():
    sql = connector(FakeConnection(schema={"TaxType": ["TaxType", "TaxRate"], "Other": ["x"]}))
    objects = {"tax": {"table": ["TaxCode", "TaxType"], "columns": {"code": ["TaxCode", "TaxType"], "rate": "TaxRate"}}}
    mapping, problems = sql.resolve_objects(objects)
    assert problems == []
    assert render_query("SELECT {tax.code} FROM {tax} WHERE {tax.rate} = 6", mapping) == \
        "SELECT [TaxType] FROM [TaxType] WHERE [TaxRate] = 6"
    _, problems = sql.resolve_objects({"s": {"table": "StockDTL", "columns": {"q": "Qty"}}})
    assert problems == ["table StockDTL"]
    _, problems = sql.resolve_objects({"o": {"table": "Other", "columns": {"q": ["Qty", "Quantity"]}}})
    assert problems == ["Other.Qty|Quantity"]


def test_server_info_reports_express_usage():
    conn = FakeConnection(files=[{"type_desc": "ROWS", "size_mb": 5120}, {"type_desc": "LOG", "size_mb": 100}])
    info = connector(conn).server_info()
    assert info["release"] == "2022" and info["is_express"] and info["express_used_pct"] == 50.0
    assert info["log_mb"] == 100


def test_unreachable_server_reports_error_without_password(monkeypatch):
    monkeypatch.setenv("AC_SQL_USER", "ac_readonly")
    monkeypatch.setenv("AC_SQL_PASSWORD", "topsecret")

    def boom(conn_str):
        assert "topsecret" in conn_str
        raise SqlUnavailable("Could not connect to SQL Server: error 26")

    ok, error = SqlServerConnector(Settings(), Masker(), None, connect=boom).reachable()
    assert not ok and "26" in error and "topsecret" not in error


# ---- logs --------------------------------------------------------------------

def test_log_window_ignores_old_lines(tmp_path):
    now = dt.datetime.now()
    old, new = now - dt.timedelta(days=90), now - dt.timedelta(hours=1)
    (tmp_path / "error.log").write_text(
        f"{old:%Y-%m-%d %H:%M:%S} ERROR Login failed for user 'x'\n"
        f"{new:%Y-%m-%d %H:%M:%S} ERROR Login failed for user 'y'\n   at Stack.Frame()\n")
    logs = LogConnector([tmp_path], Masker(), since=now - dt.timedelta(days=30))
    found = logs.find("Login failed", context=2)
    assert len(found.hits) == 1 and found.older_ignored == 1 and found.hits[0].line_no == 2
    assert found.hits[0].context == ["at Stack.Frame()"]
    assert len(LogConnector([tmp_path], Masker()).find("Login failed").hits) == 2


def test_day_first_timestamps():
    assert parse_timestamp("13/11/2026 2:05:09 PM Error") == dt.datetime(2026, 11, 13, 14, 5, 9)
    assert parse_timestamp("no time here") is None


def test_parse_since():
    now = dt.datetime(2026, 11, 16, 12, 0)
    s = Settings(log_days=30)
    assert parse_since(None, s, now) == now - dt.timedelta(days=30)
    assert parse_since("12h", s, now) == now - dt.timedelta(hours=12)
    assert parse_since("all", s, now) is None
    assert parse_since("2026-11-01", s, now) == dt.datetime(2026, 11, 1)
    with pytest.raises(ConfigError):
        parse_since("soon", s, now)


# ---- config, playbooks, knowledge -------------------------------------------

def test_config_rejects_unknown_keys_and_wrong_types(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text("log_dayz = 3\n")
    with pytest.raises(ConfigError, match="Unknown setting"):
        load_settings(cfg)
    cfg.write_text('query_timeout = "long"\n')
    with pytest.raises(ConfigError, match="whole number"):
        load_settings(cfg)


def test_built_in_playbooks_are_complete():
    playbooks, problems = load_playbooks()
    assert problems == [] and len(playbooks) == 8
    for pb in playbooks:
        assert pb.ask_customer, pb.id
        for cause in pb.causes:
            assert cause.get("why") and cause.get("verify"), (pb.id, cause["id"])
        for check in pb.checks:
            if check["type"] == "sql":
                assert check.get("verified") is False, "AutoCount table names stay flagged until confirmed"


def test_playbook_validation_catches_mistakes():
    bad = {"id": "x", "title": "t", "keywords": ["k"], "checks": [
        {"id": "a", "type": "sql", "objects": {"t": {"table": "T"}}, "query": "DELETE FROM {t}"},
        {"id": "b", "type": "sql", "query": "SELECT {nope} FROM x"},
        {"id": "c", "type": "log", "pattern": "("},
        {"id": "d", "type": "magic"}],
        "causes": [{"id": "c1", "title": "t", "menu": "m", "steps": ["s"], "checks": ["zzz"]}]}
    text = " ".join(validate_playbook(bad))
    for expected in ("not a read-only SELECT", "{nope}", "not a valid pattern", "unknown type 'magic'",
                     "unknown check 'zzz'"):
        assert expected in text


def test_broken_local_playbook_is_skipped_and_reported(tmp_path):
    (tmp_path / "mine.yaml").write_text(yaml.safe_dump({"id": "mine", "title": "x"}))
    playbooks, problems = load_playbooks(tmp_path)
    assert len(playbooks) == 8 and "mine.yaml: skipped" in problems[0]


def test_error_codes_match_question_text(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.sqlite", Masker())
    hits = store.match_error_codes([("your question", "restore fails: backed up on a server running version 16")])
    assert [h["code"] for h in hits] == ["SQL 3169"]
    assert store.match_error_codes([("your question", "invoice 3169 for RM 547.00")]) == []


# ---- commands ----------------------------------------------------------------

def run(argv, env, capsys):
    rc = main(argv, connect=lambda _s: env.connection, check_location=False)
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_question_without_subcommand_is_ask(tmp_path, monkeypatch, capsys):
    env = setup(tmp_path, monkeypatch)
    rc, out, _ = run(["stock balance mismatch", "--db", "AED_DEMO", "--ticket-note"], env, capsys)
    assert rc == 0 and "#1 [" in out and "Why:" in out and "Check it worked:" in out
    assert "Ask the customer:" in out and "--- Ticket note (masked) ---" in out


def test_ask_report_carries_why_verify_and_login(tmp_path, monkeypatch, capsys):
    env = setup(tmp_path, monkeypatch)
    _, out, _ = run(["ask", "stock balance mismatch", "--db", "AED_DEMO", "--json"], env, capsys)
    report = json.loads(out)
    top = report["recommendations"][0]
    assert top["why"] and top["verify"] and top["safety"]
    assert report["login"] == {"login_name": "ac_readonly", "read_only": True, "write_rights": []}
    assert report["questions_for_customer"] and report["tool_version"]


def test_ask_with_writer_login_skips_database_and_says_why(tmp_path, monkeypatch, capsys):
    env = setup(tmp_path, monkeypatch)
    env.connection.login.update(WRITER)
    _, out, _ = run(["ask", "stock balance mismatch", "--db", "AED_DEMO", "--json"], env, capsys)
    report = json.loads(out)
    assert any("can change data" in n for n in report["notes"])
    assert all(not r["evidence"] for r in report["recommendations"])


def test_query_command_is_guarded_and_masked(tmp_path, monkeypatch, capsys):
    env = setup(tmp_path, monkeypatch)
    env.connection.responses.insert(0, (__import__("re").compile("FROM Debtor"), [
        {"AccNo": "300-A001", "CompanyName": "Kedai Ah Kow Sdn Bhd", "Remark": "call 012-3456789"}]))
    rc, out, _ = run(["query", "SELECT AccNo, CompanyName, Remark FROM Debtor", "--db", "AED_DEMO"], env, capsys)
    assert rc == 0 and "300-A001" in out and "Ah Kow" not in out and "012-3456789" not in out
    rc, _, err = run(["query", "UPDATE Debtor SET CompanyName = 'x'", "--db", "AED_DEMO"], env, capsys)
    assert rc == 2 and "Refused" in err
    assert not any("UPDATE Debtor" in q for q in env.connection.queries)


def test_logs_config_schema_commands(tmp_path, monkeypatch, capsys):
    env = setup(tmp_path, monkeypatch, "einvoice_submission_fails")
    rc, out, _ = run(["logs", "401", "--json"], env, capsys)
    hits = json.loads(out)["hits"]
    assert rc == 0 and len(hits) == 2 and "ab12cd34" not in out
    rc, out, _ = run(["config", "EInvoice"], env, capsys)
    assert "EInvoiceEnvironment = Production" in out and "do-not-show" not in out
    rc, out, _ = run(["schema", "tin", "--db", "AED_DEMO"], env, capsys)
    assert rc == 0 and "TaxEntity: TaxEntityID (nvarchar), TIN (nvarchar)" in out


def test_scan_reports_server_and_login(tmp_path, monkeypatch, capsys):
    env = setup(tmp_path, monkeypatch)
    rc, out, _ = run(["scan", "--db", "AED_DEMO", "--json"], env, capsys)
    scan = json.loads(out)
    db = scan["database"]
    assert db["login"]["read_only"] and db["sql_server"]["release"] == "2022"
    assert db["largest_tables"][0] == {"table_name": "StockDTL", "row_count": 184220}
    assert db["version_tables"] == ["DBVersion"]
    rc, out, _ = run(["scan", "--db", "AED_DEMO"], env, capsys)
    assert "(read-only)" in out and "Express limit" in out


def test_check_playbooks_against_a_database(tmp_path, monkeypatch, capsys):
    env = setup(tmp_path, monkeypatch)
    env.connection.schema["StockDetail2"] = {"x": "int"}
    del env.connection.schema["ItemUOM"]
    rc, out, _ = run(["check-playbooks", "--db", "AED_DEMO"], env, capsys)
    assert rc == 0
    assert "[OK]      stock_balance_mismatch.negative_balance" in out
    assert "[MISSING] stock_balance_mismatch.bad_uom_rate: table ItemUOM" in out
    assert "[MISSING] sst_report_vs_ledger.manual_je_to_tax_account" in out


def test_doctor(tmp_path, monkeypatch, capsys):
    env = setup(tmp_path, monkeypatch)
    rc, out, _ = run(["doctor", "--db", "AED_DEMO"], env, capsys)
    assert rc == 0 and "[PASS] Read-only login" in out and "[PASS] AutoCount program" in out
    env.connection.login.update(WRITER)
    rc, out, _ = run(["doctor", "--db", "AED_DEMO"], env, capsys)
    assert rc == 1 and "[FAIL] Read-only login" in out and "setup-login" in out


def test_doctor_reports_unreachable_server(tmp_path):
    def boom(_):
        raise SqlUnavailable("Could not connect to SQL Server: Login failed for user (18456)")

    findings = doctor.run_doctor(Settings(install_path=str(tmp_path), data_dir=tmp_path / "d"), connect=boom)
    assert findings[-1].status == doctor.FAIL and "18456" in findings[-1].detail


def test_setup_login_writes_script_only_with_permission(tmp_path, monkeypatch, capsys):
    env = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(setup_login, "store_credentials", lambda login, password: False)
    rc, out, err = run(["setup-login", "--book", "AED_DEMO"], env, capsys)
    assert rc == 1 and "Re-run with --yes" in err and not (tmp_path / "data").exists()
    rc, out, _ = run(["setup-login", "--book", "AED_DEMO", "--yes"], env, capsys)
    script = (tmp_path / "data" / "setup" / "create_readonly_login.sql").read_text()
    assert rc == 0 and "ALTER ROLE [db_datareader] ADD MEMBER [ac_readonly];" in script and "USE [AED_DEMO];" in script
    password = script.split("PASSWORD = N'")[1].split("'")[0]
    assert len(password) == 24 and password not in out  # never printed
    assert env.connection.queries == []  # nothing was sent to SQL Server
    rc, out, _ = run(["setup-login", "--cleanup"], env, capsys)
    assert not (tmp_path / "data" / "setup" / "create_readonly_login.sql").exists()


def test_setup_login_rejects_odd_names():
    with pytest.raises(ValueError):
        setup_login.build_script("ac'; DROP", "pw", ["AED_X"])
    with pytest.raises(ValueError):
        setup_login.build_script("ac_readonly", "pw", ["AED]; DROP DATABASE x"])
    assert "'" not in setup_login.generate_password() and ";" not in setup_login.generate_password()


def test_generated_script_is_never_runnable_through_the_tool():
    from autocount_assist.sql_guard import ReadOnlyViolation
    with pytest.raises(ReadOnlyViolation):
        check_read_only(setup_login.build_script("ac_readonly", "pw", ["AED_X"]))


def test_propose_without_scripts_saves_nothing(tmp_path, monkeypatch, capsys):
    env = setup(tmp_path, monkeypatch)
    rc, out, _ = run(["propose", "stock balance mismatch", "--db", "AED_DEMO", "--yes"], env, capsys)
    assert rc == 0 and "No data-fix scripts" in out and not (tmp_path / "data").exists()


def test_customer_report_alone_is_never_high_confidence(tmp_path, monkeypatch, capsys):
    env = setup(tmp_path, monkeypatch, "backup_restore_fails")
    _, out, _ = run(["ask", env.spec["question"], "--db", "AED_DEMO", "--json"], env, capsys)
    top = json.loads(out)["recommendations"][0]
    assert top["cause_id"] == "newer_sql_version" and top["confidence"] == "Medium"
    assert "customer reported" in top["caveat"]


def test_ticket_note_is_short_and_masked(tmp_path, monkeypatch):
    from test_recommendation_format import run as build
    note = render_ticket_note(build("einvoice_submission_fails", tmp_path))
    assert note.startswith("Issue:") and "Likely cause" in note and "ab12cd34" not in note


# ---- Windows services ----------------------------------------------------------

def test_sql_service_names_and_state():
    assert system.sql_service_names(r"localhost\A2006") == ["MSSQL$A2006", "SQLBrowser"]
    assert system.sql_service_names("(local)") == ["MSSQLSERVER"]
    assert system.is_local_server(r".\A2006") and not system.is_local_server(r"some-other-pc-xyz\A2006")
    assert system.service_state("x", lambda a: (0, "        STATE              : 4  RUNNING")) == "running"
    assert system.service_state("x", lambda a: (0, "        STATE              : 1  STOPPED")) == "stopped"
    assert system.service_state("x", lambda a: (1060, "FAILED 1060")) == "not_installed"
