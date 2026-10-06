import pytest

from autocount_assist.analyser import analyse, load_playbooks
from autocount_assist.cli import make_context
from autocount_assist.demo import build_env, scenario_ids
from autocount_assist.recommender import (Recommendation, build_report, confidence_label, render_text,
                                          validate_recommendation)


def run(sid, tmp_path, with_db=True):
    env = build_env(sid, tmp_path)
    ctx = make_context(env.settings, "AED_DEMO" if with_db else None,
                       connect=lambda _s: env.connection, check_location=False)
    return build_report(analyse(env.spec["question"], ctx, load_playbooks()), ctx)


@pytest.mark.parametrize("sid", scenario_ids())
def test_every_recommendation_meets_the_format(sid, tmp_path):
    report = run(sid, tmp_path)
    assert report.recommendations
    assert [r.rank for r in report.recommendations] == list(range(1, len(report.recommendations) + 1))
    scores = [r.confidence_score for r in report.recommendations]
    assert scores == sorted(scores, reverse=True)
    for r in report.recommendations:
        assert validate_recommendation(r) == [], r
    d = report.to_dict()
    assert set(d["recommendations"][0]) >= {"cause", "steps", "menu", "confidence", "evidence", "missing_evidence"}
    assert "Steps:" in render_text(report)


def test_validator_catches_unsupported_confidence():
    r = Recommendation(1, "pb", "c", "cause", "High", 0.9, ["s"], "menu", False, [], [], 0)
    assert "must cite evidence or say which evidence is missing" in validate_recommendation(r)
    assert "confidence above Low without evidence" in validate_recommendation(r)


def test_missing_database_is_reported_not_guessed(tmp_path):
    report = run("stock_balance_mismatch", tmp_path, with_db=False)
    assert any("Database checks skipped" in n for n in report.notes)
    for r in report.recommendations:
        assert r.confidence == "Low" and r.missing_evidence and not r.evidence


def test_unknown_question_returns_no_guess(tmp_path):
    env = build_env("stock_balance_mismatch", tmp_path)
    ctx = make_context(env.settings, None, check_location=False)
    report = build_report(analyse("printer jammed at reception", ctx, load_playbooks()), ctx)
    assert report.recommendations == [] and "No scenario playbook matched" in report.notes[0]


def test_confidence_labels():
    assert [confidence_label(x) for x in (0.9, 0.75, 0.6, 0.49)] == ["High", "High", "Medium", "Low"]
