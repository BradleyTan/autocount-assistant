"""The five required sample scenarios, end to end on synthetic data."""

import pytest

from autocount_assist.demo import scenario_ids
from test_recommendation_format import run

EXPECTED = {
    "einvoice_submission_fails": "invalid_credentials",
    "stock_balance_mismatch": "negative_stock",
    "sst_report_vs_ledger": "manual_journal",
    "login_fails_after_upgrade": "db_not_upgraded",
    "db_version_mismatch": "upgrade_this_pc",
}


def test_all_five_scenarios_ship():
    assert sorted(EXPECTED) == scenario_ids()


@pytest.mark.parametrize("sid,cause", EXPECTED.items())
def test_top_cause(sid, cause, tmp_path):
    report = run(sid, tmp_path)
    assert report.playbooks[0] == sid
    top = report.recommendations[0]
    assert top.cause_id == cause
    assert top.evidence, "top recommendation must cite evidence"


def test_secrets_in_logs_never_reach_output(tmp_path):
    out = run("einvoice_submission_fails", tmp_path).to_json()
    assert "ab12cd34" not in out and "do-not-show" not in out


def test_version_mismatch_rules_out_opposite_direction(tmp_path):
    report = run("db_version_mismatch", tmp_path)
    assert any("older than the system" in r for r in report.ruled_out)
