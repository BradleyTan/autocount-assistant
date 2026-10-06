import json

from autocount_assist.cli import main
from autocount_assist.demo import build_env


def setup(tmp_path, monkeypatch, sid="stock_balance_mismatch"):
    env = build_env(sid, tmp_path)
    cfg = tmp_path / "autocount-assist.toml"
    cfg.write_text(f'install_path = {json.dumps(env.settings.install_path)}\n'
                   f'log_dirs = ["{{install_path}}"]\n'
                   f'data_dir = {json.dumps(str(tmp_path / "data"))}\n')
    monkeypatch.setenv("AC_CONFIG", str(cfg))
    for v in ("AC_DB", "AC_SQL_USER", "AC_SQL_PASSWORD", "AC_DATA_DIR"):
        monkeypatch.delenv(v, raising=False)
    return env


def test_ask_json_writes_nothing(tmp_path, monkeypatch, capsys):
    env = setup(tmp_path, monkeypatch)
    rc = main(["ask", "stock balance mismatch", "--db", "AED_DEMO", "--json"],
              connect=lambda _s: env.connection, check_location=False)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["recommendations"][0]["cause_id"] == "negative_stock"
    assert not (tmp_path / "data").exists()


def test_learn_needs_permission(tmp_path, monkeypatch, capsys):
    setup(tmp_path, monkeypatch)
    rc = main(["learn", "case", "--scenario", "s", "--cause", "c", "--fix", "f"], check_location=False)
    assert rc == 1 and "Re-run with --yes" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


def test_learn_case_with_yes_then_listed(tmp_path, monkeypatch, capsys):
    setup(tmp_path, monkeypatch)
    assert main(["learn", "case", "--scenario", "Stock -ve for ITM-1", "--cause", "missing GRN",
                 "--fix", "posted GRN", "--playbook", "stock_balance_mismatch", "--cause-id", "negative_stock",
                 "--yes"], check_location=False) == 0
    assert (tmp_path / "data" / "kb.sqlite").exists()
    capsys.readouterr()
    main(["cases", "--json"], check_location=False)
    assert json.loads(capsys.readouterr().out)[0]["cause_id"] == "negative_stock"


def test_confirmed_case_raises_confidence(tmp_path, monkeypatch, capsys):
    env = setup(tmp_path, monkeypatch)
    args = ["ask", "stock balance mismatch", "--db", "AED_DEMO", "--json"]
    main(args, connect=lambda _s: env.connection, check_location=False)
    before = json.loads(capsys.readouterr().out)["recommendations"][0]["confidence_score"]
    main(["learn", "case", "--scenario", "stock balance wrong", "--cause", "neg", "--fix", "grn",
          "--playbook", "stock_balance_mismatch", "--cause-id", "negative_stock", "--yes"], check_location=False)
    capsys.readouterr()
    main(args, connect=lambda _s: env.connection, check_location=False)
    after = json.loads(capsys.readouterr().out)["recommendations"][0]
    assert after["confidence_score"] > before and after["past_confirmed_cases"] == 1


def test_credentials_in_config_file_rejected(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text('password = "x"\n')
    monkeypatch.setenv("AC_CONFIG", str(cfg))
    assert main(["cases"]) == 2 and "environment variables" in capsys.readouterr().err


def test_ask_without_db_non_interactive(tmp_path, monkeypatch, capsys):
    setup(tmp_path, monkeypatch)
    from autocount_assist.fakedb import FakeConnection
    rc = main(["ask", "stock balance mismatch", "--json"], connect=lambda _s: FakeConnection(), check_location=False)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["database"] is None
    assert any("pass --db" in n for n in out["notes"])


def test_scan_json(tmp_path, monkeypatch, capsys):
    env = setup(tmp_path, monkeypatch, "login_fails_after_upgrade")
    main(["scan", "--db", "AED_DEMO", "--json"], connect=lambda _s: env.connection, check_location=False)
    out = json.loads(capsys.readouterr().out)
    assert out["versions"]["compatibility"] == "mismatch" and out["versions"]["direction"] == "db_older"
    assert out["recent_errors"]


def test_demo_runs_all(capsys):
    assert main(["demo", "all"]) == 0
    assert capsys.readouterr().out.count("=== Demo:") == 5
