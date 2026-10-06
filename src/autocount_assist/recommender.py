"""Turn an Analysis into ranked, evidence-backed recommendations (text or JSON)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .analyser import Analysis, Context

HIGH = 0.75
MEDIUM = 0.5


def confidence_label(score: float) -> str:
    return "High" if score >= HIGH else "Medium" if score >= MEDIUM else "Low"


@dataclass
class Recommendation:
    rank: int
    playbook: str
    cause_id: str
    cause: str
    confidence: str
    confidence_score: float
    steps: list[str]
    menu: str
    menu_verified: bool
    evidence: list[dict[str, str]]
    missing_evidence: list[str]
    past_confirmed_cases: int
    proposed_change: str | None = None  # SQL text; never executed, saved only with permission
    caveat: str = ""


@dataclass
class Report:
    question: str
    database: str | None
    environment: dict[str, Any]
    playbooks: list[str]
    recommendations: list[Recommendation]
    ruled_out: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    related_knowledge: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)


def build_report(analysis: Analysis, ctx: Context, max_recs: int = 5) -> Report:
    v = ctx.version
    env = {"system_version": v.app_version, "database_version": v.db_version,
           "expected_database_version": v.expected_db, "compatibility": v.status,
           "direction": v.direction, "sql_server": ctx.settings.sql_server}
    recs: list[Recommendation] = []
    for i, sc in enumerate(analysis.causes[:max_recs], start=1):
        c = sc.cause
        evidence, seen = [], set()
        for r in sc.supporting:
            for e in r.evidence:
                if (e.ref, e.detail) not in seen:
                    seen.add((e.ref, e.detail))
                    evidence.append(e.to_dict() | {"check": r.check_id, "verified": r.verified})
        missing = [f"{r.description}: {r.note}" for r in sc.missing]
        caveat = ""
        if sc.advice_only:
            caveat = "No live check exists for this cause; it is general guidance, not evidence from this system."
            missing.append("No automated check for this cause")
        elif not sc.supporting:
            caveat = "Evidence for this cause could not be collected, so it is neither confirmed nor ruled out."
        elif any(not r.verified for r in sc.supporting):
            caveat = "Some evidence relies on AutoCount table names not yet confirmed on this database."
        recs.append(Recommendation(
            rank=i, playbook=sc.playbook.id, cause_id=c["id"], cause=c["title"],
            confidence=confidence_label(sc.score), confidence_score=round(sc.score, 2),
            steps=list(c.get("steps", [])), menu=c.get("menu", ""),
            menu_verified=bool(c.get("menu_verified", False)), evidence=evidence,
            missing_evidence=missing, past_confirmed_cases=sc.past_cases,
            proposed_change=c.get("proposed_sql"), caveat=caveat))
    notes: list[str] = []
    if not analysis.playbooks:
        notes.append("No scenario playbook matched this question. Showing related knowledge only; "
                     "try rephrasing or pass --playbook.")
    elif not any(r.evidence for r in recs):
        notes.append("No supporting evidence was found on this system. The causes below are unconfirmed; "
                     "do not act on them without checking manually.")
    if ctx.sql is None:
        notes.append(f"Database checks skipped: {ctx.sql_error or 'no account book selected'}.")
    return Report(
        question=analysis.question, database=ctx.sql.database if ctx.sql else None, environment=env,
        playbooks=[p.id for p in analysis.playbooks], recommendations=recs,
        ruled_out=[f"{pb.id}: {c['title']}" for pb, c in analysis.ruled_out],
        checks=[{"id": k} | r.to_dict() for k, r in analysis.checks.items()],
        related_knowledge=analysis.related, notes=notes)


def validate_recommendation(rec: Recommendation) -> list[str]:
    """Format contract every recommendation must meet. Returns a list of problems (empty = valid)."""
    problems = []
    if rec.rank < 1:
        problems.append("rank must start at 1")
    if not rec.cause:
        problems.append("cause is empty")
    if not rec.steps:
        problems.append("steps are empty")
    if not rec.menu:
        problems.append("menu/screen is empty")
    if rec.confidence != confidence_label(rec.confidence_score):
        problems.append("confidence label does not match score")
    if not rec.evidence and not rec.missing_evidence:
        problems.append("must cite evidence or say which evidence is missing")
    if not rec.evidence and rec.confidence != "Low":
        problems.append("confidence above Low without evidence")
    for e in rec.evidence:
        if not (e.get("source") and e.get("ref") and e.get("detail")):
            problems.append(f"evidence entry incomplete: {e}")
    return problems


def render_text(report: Report) -> str:
    lines: list[str] = []
    env = report.environment
    lines.append(f"Question: {report.question}")
    lines.append(f"System {env['system_version'] or '?'} | Database {env['database_version'] or '?'} "
                 f"(expected {env['expected_database_version'] or '?'}) | compatibility: {env['compatibility']}"
                 + (f" ({env['direction']})" if env["direction"] else ""))
    lines.append(f"Account book: {report.database or '(none)'} | Playbooks: {', '.join(report.playbooks) or '(none)'}")
    for n in report.notes:
        lines.append(f"! {n}")
    lines.append("")
    if not report.recommendations:
        lines.append("No recommendations: there is not enough evidence to suggest a cause.")
    for r in report.recommendations:
        lines.append(f"#{r.rank} [{r.confidence} {r.confidence_score:.2f}] {r.cause}")
        menu = r.menu + ("" if r.menu_verified else "  (menu path to confirm)")
        lines.append(f"   Where: {menu}")
        lines.append("   Steps:")
        lines.extend(f"     {i}. {s}" for i, s in enumerate(r.steps, start=1))
        if r.evidence:
            lines.append("   Evidence:")
            for e in r.evidence:
                tag = "" if e.get("verified", True) else " [unverified table]"
                lines.append(f"     - {e['source']}: {e['ref']}{tag}\n         {e['detail']}")
        if r.missing_evidence:
            lines.append("   Missing evidence:")
            lines.extend(f"     - {m}" for m in r.missing_evidence)
        if r.past_confirmed_cases:
            lines.append(f"   Confirmed in {r.past_confirmed_cases} past case(s).")
        if r.proposed_change:
            lines.append("   A data fix script is available (not run). Use --save-proposal to save it for review.")
        if r.caveat:
            lines.append(f"   Note: {r.caveat}")
        lines.append("")
    if report.ruled_out:
        lines.append("Ruled out by checks: " + "; ".join(report.ruled_out))
    if report.related_knowledge:
        lines.append("Related knowledge:")
        lines.extend(f"  - [{k['kind']}] {k['title']}: {k['snippet']}" for k in report.related_knowledge)
    return "\n".join(lines).rstrip() + "\n"
