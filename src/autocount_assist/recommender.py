"""Turn an Analysis into ranked, evidence-backed recommendations (text, JSON or a ticket note)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from . import __version__
from .analyser import Analysis, Context
from .connectors.sqlserver import SqlUnavailable

HIGH = 0.75
MEDIUM = 0.5
DEFAULT_SAFETY = "These steps change data or settings. Back up the account book first and get the customer's agreement."


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
    evidence: list[dict[str, Any]]
    missing_evidence: list[str]
    past_confirmed_cases: int
    proposed_change: str | None = None  # SQL text; never executed, saved only with permission
    caveat: str = ""
    why: str = ""  # the reasoning behind the cause, in accounting or system terms
    verify: list[str] = field(default_factory=list)  # how to confirm the fix worked
    safety: str = ""  # what to do before changing anything


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
    questions_for_customer: list[str] = field(default_factory=list)
    error_codes: list[dict[str, Any]] = field(default_factory=list)
    login: dict[str, Any] | None = None
    tool_version: str = __version__

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)


def _login_summary(ctx: Context) -> tuple[dict[str, Any] | None, str | None]:
    if ctx.sql is None:
        return None, None
    try:
        info = ctx.sql.login_info()
    except SqlUnavailable:
        return None, None
    summary = {"login_name": info.get("login_name"), "read_only": not info.get("can_write"),
               "write_rights": info.get("write_rights", [])}
    warning = None
    if info.get("can_write"):
        warning = (f"The login '{info.get('login_name')}' can change data ({', '.join(info['write_rights'])}). "
                   "This tool only sends SELECT statements, but a read-only login is safer: "
                   "run `autocount-assist setup-login`.")
    return summary, warning


def build_report(analysis: Analysis, ctx: Context, max_recs: int = 5,
                 playbook_problems: list[str] | None = None) -> Report:
    v = ctx.version
    env = {"system_version": v.app_version, "database_version": v.db_version,
           "expected_database_version": v.expected_db, "compatibility": v.status,
           "direction": v.direction, "sql_server": ctx.settings.sql_server, "log_window": ctx.log_window}
    recs: list[Recommendation] = []
    for i, sc in enumerate(analysis.causes[:max_recs], start=1):
        c = sc.cause
        evidence: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
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
        elif all(r.reported_only for r in sc.supporting):
            caveat = "This rests on what the customer reported, not on anything found on this system."
        elif any(not r.verified for r in sc.supporting):
            caveat = "Some evidence relies on AutoCount table names not yet confirmed on this database."
        changes_data = bool(c.get("proposed_sql")) or c.get("changes_data", True)
        recs.append(Recommendation(
            rank=i, playbook=sc.playbook.id, cause_id=c["id"], cause=c["title"],
            confidence=confidence_label(sc.score), confidence_score=round(sc.score, 2),
            steps=list(c.get("steps", [])), menu=c.get("menu", ""),
            menu_verified=bool(c.get("menu_verified", False)), evidence=evidence,
            missing_evidence=missing, past_confirmed_cases=sc.past_cases,
            proposed_change=c.get("proposed_sql"), caveat=caveat, why=str(c.get("why", "")).strip(),
            verify=list(c.get("verify", [])),
            safety=str(c.get("safety") or (DEFAULT_SAFETY if changes_data else "")).strip()))
    notes: list[str] = list(playbook_problems or [])
    if not analysis.playbooks:
        notes.append("No scenario playbook matched this question. Showing related knowledge only; "
                     "try rephrasing or pass --playbook.")
    elif not any(r.evidence for r in recs):
        notes.append("No supporting evidence was found on this system. The causes below are unconfirmed; "
                     "do not act on them without checking manually.")
    if ctx.sql is None:
        notes.append(f"Database checks skipped: {ctx.sql_error or 'no account book selected'}.")
    login, warning = _login_summary(ctx)
    if warning:
        notes.append(warning)
    questions: list[str] = []
    for pb in analysis.playbooks:
        questions.extend(q for q in pb.ask_customer if q not in questions)
    return Report(
        question=analysis.question, database=ctx.sql.database if ctx.sql else None, environment=env,
        playbooks=[p.id for p in analysis.playbooks], recommendations=recs,
        ruled_out=[f"{pb.id}: {c['title']}" for pb, c in analysis.ruled_out],
        checks=[{"id": k} | r.to_dict() for k, r in analysis.checks.items()],
        related_knowledge=analysis.related, notes=notes, questions_for_customer=questions,
        error_codes=analysis.error_codes, login=login)


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
    if rec.evidence and all(e.get("source") == "question" for e in rec.evidence) and rec.confidence == "High":
        problems.append("High confidence on the customer's report alone")
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
    lines.append(f"Account book: {report.database or '(none)'} | Playbooks: {', '.join(report.playbooks) or '(none)'}"
                 f" | Logs: {env.get('log_window', 'all dates')}")
    for n in report.notes:
        lines.append(f"! {n}")
    lines.append("")
    if report.error_codes:
        lines.append("Known error codes seen:")
        for c in report.error_codes:
            lines.append(f"  - {c['code']} ({c['source']}) in {c['matched_in']}: {c['cause']}")
            lines.append(f"      Fix: {c['fix']}")
        lines.append("")
    if not report.recommendations:
        lines.append("No recommendations: there is not enough evidence to suggest a cause.")
    for r in report.recommendations:
        lines.append(f"#{r.rank} [{r.confidence} {r.confidence_score:.2f}] {r.cause}")
        if r.why:
            lines.append(f"   Why: {r.why}")
        menu = r.menu + ("" if r.menu_verified else "  (menu path to confirm)")
        lines.append(f"   Where: {menu}")
        if r.safety:
            lines.append(f"   Before you change anything: {r.safety}")
        lines.append("   Steps:")
        lines.extend(f"     {i}. {s}" for i, s in enumerate(r.steps, start=1))
        if r.verify:
            lines.append("   Check it worked:")
            lines.extend(f"     - {s}" for s in r.verify)
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
            lines.append("   A data fix script is available (not run). `autocount-assist propose` saves it for review.")
        if r.caveat:
            lines.append(f"   Note: {r.caveat}")
        lines.append("")
    if report.questions_for_customer:
        lines.append("Ask the customer:")
        lines.extend(f"  - {q}" for q in report.questions_for_customer)
    if report.ruled_out:
        lines.append("Ruled out by checks: " + "; ".join(report.ruled_out))
    if report.related_knowledge:
        lines.append("Related knowledge:")
        lines.extend(f"  - [{k['kind']}] {k['title']}: {k['snippet']}" for k in report.related_knowledge)
    return "\n".join(lines).rstrip() + "\n"


def render_ticket_note(report: Report) -> str:
    """A short note to paste into a support ticket. Contains masked data only."""
    env = report.environment
    lines = [f"Issue: {report.question}",
             f"Environment: AutoCount {env['system_version'] or '?'}, database {env['database_version'] or '?'}, "
             f"account book {report.database or 'not checked'}"]
    top = report.recommendations[0] if report.recommendations else None
    if top is None:
        lines.append("Finding: no cause could be suggested from the evidence available.")
    else:
        lines.append(f"Likely cause ({top.confidence} confidence): {top.cause}")
        if top.evidence:
            lines.append("Evidence:")
            lines.extend(f"  - {e['ref']}: {e['detail']}" for e in top.evidence[:3])
        else:
            lines.append("Evidence: none found on the system; cause not confirmed.")
        lines.append("Recommended steps:")
        lines.extend(f"  {i}. {s}" for i, s in enumerate(top.steps, start=1))
        if top.verify:
            lines.append("Check after the fix: " + " ".join(top.verify))
    if report.questions_for_customer:
        lines.append("Still to ask the customer: " + " ".join(report.questions_for_customer[:3]))
    return "\n".join(lines) + "\n"
