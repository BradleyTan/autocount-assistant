"""Match a question to playbooks, run their checks against the live environment, score causes."""

from __future__ import annotations

import datetime as dt
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .connectors import system
from .connectors.config_files import ConfigConnector
from .connectors.logs import LogConnector
from .connectors.plugins import PluginConnector
from .connectors.sqlserver import SqlServerConnector, SqlUnavailable, render_query
from .connectors.version import VersionConnector, VersionReport
from .knowledge.store import KnowledgeStore
from .masking import Masker
from .models import CheckResult, Evidence
from .playbook import Playbook, load_playbooks, sql_variants
from .sql_guard import ReadOnlyViolation

__all__ = ["Analysis", "Context", "Playbook", "ScoredCause", "analyse", "load_playbooks", "run_check",
           "select_playbooks"]

FOUND_WEIGHT = 0.2
QUESTION_WEIGHT = 0.5  # what the customer reports counts for less than what the system shows
CASE_BOOST = 0.05
MAX_CASE_BOOST = 0.15
MAX_SCORE = 0.95
REPORTED_ONLY_CAP = 0.7  # never "High" on the customer's words alone

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    ">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt,
    "==": operator.eq, "!=": operator.ne,
}


@dataclass
class Context:
    settings: Settings
    masker: Masker
    logs: LogConnector
    config: ConfigConnector
    plugins: PluginConnector
    sql: SqlServerConnector | None
    store: KnowledgeStore
    server: SqlServerConnector | None = None  # instance-level connection (no account book)
    _version: VersionReport | None = None
    _server_info: dict[str, Any] | None = None
    sql_error: str | None = None
    service_state: Callable[[str], str] = system.service_state
    question: str = ""

    @property
    def version(self) -> VersionReport:
        if self._version is None:
            self._version = VersionConnector(self.settings, self.plugins, self.sql).check()
        return self._version

    @property
    def server_info(self) -> dict[str, Any]:
        if self._server_info is None:
            if self.sql is None:
                raise SqlUnavailable(self.sql_error or "No account book database selected")
            self._server_info = self.sql.server_info()
        return self._server_info

    @property
    def log_window(self) -> str:
        since = self.logs.since
        return "all dates" if since is None else f"since {since:%Y-%m-%d %H:%M}"


@dataclass
class ScoredCause:
    playbook: Playbook
    cause: dict[str, Any]
    score: float
    supporting: list[CheckResult]
    missing: list[CheckResult]
    past_cases: int
    advice_only: bool = False


@dataclass
class Analysis:
    question: str
    playbooks: list[Playbook]
    checks: dict[str, CheckResult] = field(default_factory=dict)
    causes: list[ScoredCause] = field(default_factory=list)
    ruled_out: list[tuple[Playbook, dict[str, Any]]] = field(default_factory=list)
    related: list[dict[str, Any]] = field(default_factory=list)
    error_codes: list[dict[str, Any]] = field(default_factory=list)


def since_from_days(days: int, now: dt.datetime | None = None) -> dt.datetime | None:
    return None if days <= 0 else (now or dt.datetime.now()) - dt.timedelta(days=days)


def select_playbooks(question: str, playbooks: list[Playbook], store: KnowledgeStore,
                     forced: str | None = None, top: int = 2) -> list[Playbook]:
    if forced:
        return [p for p in playbooks if p.id == forced]
    q = question.lower()
    scores: dict[str, float] = {}
    for p in playbooks:
        s = sum(1.0 for k in p.keywords if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", q))
        if s:
            scores[p.id] = s
    for hit in store.search(question, limit=10, kinds=("case",)):  # past confirmed cases vote too
        case = store.case(int(hit["ref"]))
        if case and case["playbook_id"]:
            scores[case["playbook_id"]] = scores.get(case["playbook_id"], 0) + 0.5
    ranked = sorted(scores, key=lambda pid: scores[pid], reverse=True)
    best = scores[ranked[0]] if ranked else 0
    chosen = [p for p in playbooks if p.id in ranked[:top] and scores[p.id] >= max(1.0, best / 2)]
    return sorted(chosen, key=lambda p: scores[p.id], reverse=True)


# ---- checks -------------------------------------------------------------

def run_check(check: dict[str, Any], ctx: Context) -> CheckResult:
    kind = check["type"]
    res = CheckResult(check["id"], "missing", check.get("describe", check["id"]),
                      verified=check.get("verified", True))
    try:
        runner = _RUNNERS.get(kind)
        if runner is None:
            res.note = f"Unknown check type '{kind}'"
        else:
            runner(check, ctx, res)
    except SqlUnavailable as e:
        if kind != "sql_connect":
            ctx.sql_error = str(e)
            ctx.sql = None
        res.status, res.note = "missing", str(e)
    except ReadOnlyViolation as e:  # a playbook bug: never run, report it
        res.status, res.note = "missing", f"Blocked by read-only guard: {e}"
    except Exception as e:  # one broken check must not sink the whole answer
        res.status, res.note = "missing", f"Check failed: {type(e).__name__}: {e}"
    return res


def _check_log(check: dict[str, Any], ctx: Context, res: CheckResult) -> None:
    if not ctx.logs.discover():
        res.note = "No AutoCount log files found in the configured log folders"
        return
    found = ctx.logs.find(check["pattern"], limit=check.get("limit", 5), context=check.get("context", 0))
    res.status = "found" if found.hits else "clear"
    res.evidence = [Evidence("log", h.ref, " | ".join([h.line, *h.context])) for h in found.hits]
    res.note = f"Searched {found.files_searched} log file(s), {ctx.log_window}"
    if found.older_ignored:
        res.note += f"; {found.older_ignored} older match(es) ignored"
    if found.files_too_old:
        res.note += f"; {found.files_too_old} older file(s) not searched"


def _check_config(check: dict[str, Any], ctx: Context, res: CheckResult) -> None:
    if not ctx.config.entries():
        res.note = "No readable config files found"
        return
    keyed = ctx.config.find(check["key"])
    if check.get("value"):
        if not keyed:  # the setting isn't in any file we can read: unknown, not "clear"
            res.note = "Setting not found in the config files read"
            return
        hits = ctx.config.find(check["key"], check["value"])
        res.status = "found" if hits else "clear"
    else:
        hits = keyed
        want_present = check.get("finding_when", "present") == "present"
        res.status = "found" if bool(hits) == want_present else "clear"
    res.evidence = [Evidence("config", f"{ctx.masker.mask_path(e.file)} :: {e.key}", e.value) for e in hits[:5]]
    if res.status == "found" and not hits:
        res.evidence = [Evidence("config", check["key"], "setting not present in any config file")]


def _check_sql(check: dict[str, Any], ctx: Context, res: CheckResult) -> None:
    if ctx.sql is None:
        res.note = ctx.sql_error or "No account book database selected"
        return
    problems: list[str] = []
    for variant in sql_variants(check):
        mapping, missing = ctx.sql.resolve_objects(variant.get("objects"))
        if missing:
            problems.append(", ".join(missing))
            continue
        rows = ctx.sql.query(render_query(variant["query"], mapping), unmasked=check.get("unmasked", ()))
        when = variant.get("finding_when", check.get("finding_when", "rows"))
        if when.startswith("nonzero:"):
            col = when.split(":", 1)[1]
            found = any(_number(r.get(col)) > 0 for r in rows)
        else:
            found = bool(rows)
        res.status = "found" if found else "clear"
        ref = variant.get("ref") or check.get("ref") or "query"
        res.evidence = [Evidence("sql", ref, _fmt_row(r)) for r in rows[:5]] if found else []
        res.note = f"Ran read-only query on {ctx.sql.database}"
        return
    res.note = f"Not found in this database: {' / '.join(problems)}"
    if not res.verified:
        res.note += " (AutoCount table name not yet confirmed; run `autocount-assist check-playbooks --db`)"


def _check_question(check: dict[str, Any], ctx: Context, res: CheckResult) -> None:
    m = re.search(check["pattern"], ctx.question, re.IGNORECASE)
    res.status = "found" if m else "clear"
    res.reported_only = True
    if m:
        res.evidence = [Evidence("question", "reported by the customer",
                                 ctx.masker.mask_text(m.group(0).strip()) or "")]


def _check_version(check: dict[str, Any], ctx: Context, res: CheckResult) -> None:
    v = ctx.version
    if v.status == "unknown":
        res.note = "; ".join(v.notes) or "Version unknown"
        return
    want = check.get("direction", "any")
    found = v.status == "mismatch" and (want == "any" or v.direction == want)
    res.status = "found" if found else "clear"
    res.evidence = [Evidence("version", "system vs database",
                             f"system {v.app_version} ({ctx.masker.mask_path(v.app_source)}); database "
                             f"{v.db_version} ({v.db_source}); expected database {v.expected_db}")]


def _check_core_dlls(check: dict[str, Any], ctx: Context, res: CheckResult) -> None:
    app = ctx.version.app_version
    if not app:
        res.note = "System version unknown, cannot compare DLL versions"
        return
    if not ctx.plugins.list_binaries():
        res.note = "No DLLs found under the configured plugin folders"
        return
    bad = ctx.plugins.core_version_mismatches(app)
    res.status = "found" if bad else "clear"
    res.evidence = [Evidence("plugin", ctx.masker.mask_path(b.path),
                             f"file version {b.file_version}, system is {app}") for b in bad[:5]]


def _check_server_fact(check: dict[str, Any], ctx: Context, res: CheckResult) -> None:
    info = dict(ctx.server_info)
    data_mb = info.get("data_mb") or 0
    info["log_to_data_ratio"] = round((info.get("log_mb") or 0) / data_mb, 2) if data_mb else None
    value = info.get(check["fact"])
    if value is None:
        res.note = f"{check['fact']} does not apply to this server"
        res.status = "clear" if check["fact"] == "express_used_pct" else "missing"
        return
    want = check["value"]
    if isinstance(value, str) and isinstance(want, str):
        value, want = value.upper(), want.upper()
    res.status = "found" if _OPS[check["op"]](value, want) else "clear"
    res.note = f"Read SQL Server properties of {info.get('database_name')}"
    res.evidence = [Evidence("sql", "SQL Server and database properties", describe_server(info))]


def describe_server(info: dict[str, Any]) -> str:
    parts = [f"SQL Server {info.get('release') or info.get('product_version')} {info.get('edition')}",
             f"data {info.get('data_mb')} MB", f"log {info.get('log_mb')} MB",
             f"recovery {info.get('recovery_model')}", f"status {info.get('status')}"]
    if info.get("is_express"):
        parts.insert(2, f"{info.get('express_used_pct')}% of the 10 GB Express limit")
    return ", ".join(parts)


def _check_sql_connect(check: dict[str, Any], ctx: Context, res: CheckResult) -> None:
    if ctx.server is None:
        res.note = "No SQL Server connection configured"
        return
    ok, error = ctx.server.reachable()
    pattern = check.get("error_pattern")
    res.status = "found" if not ok and (not pattern or re.search(pattern, error, re.IGNORECASE)) else "clear"
    res.note = f"Tried to connect to {ctx.settings.sql_server} from this PC with the tool's login"
    if res.status == "found":
        res.evidence = [Evidence("sql", ctx.settings.sql_server, ctx.masker.mask_text(error) or "")]


def _check_windows_service(check: dict[str, Any], ctx: Context, res: CheckResult) -> None:
    server = ctx.settings.sql_server
    if not system.is_local_server(server):
        res.note = f"SQL Server runs on another PC ({system.split_server(server)[0]}); check its services there"
        return
    names = system.sql_service_names(server)
    if check.get("service") == "browser":
        names = [n for n in names if n == "SQLBrowser"]
        if not names:
            res.status, res.note = "clear", "A default instance does not need SQL Server Browser"
            return
    elif check.get("service") == "engine":
        names = names[:1]
    states = {n: ctx.service_state(n) for n in names}
    if all(s == "unknown" for s in states.values()):
        res.note = "Windows service state could not be read on this PC"
        return
    bad = {n: s for n, s in states.items() if s not in ("running", "unknown")}
    res.status = "found" if bad else "clear"
    res.note = "Read Windows service state (sc query)"
    res.evidence = [Evidence("service", n, f"service is {s.replace('_', ' ')}") for n, s in bad.items()]


_RUNNERS: dict[str, Callable[[dict[str, Any], Context, CheckResult], None]] = {
    "log": _check_log, "config": _check_config, "sql": _check_sql, "question": _check_question,
    "version_mismatch": _check_version, "core_dll_mismatch": _check_core_dlls,
    "server_fact": _check_server_fact, "sql_connect": _check_sql_connect,
    "windows_service": _check_windows_service,
}


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt_row(row: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in row.items())


# ---- scoring ------------------------------------------------------------

def analyse(question: str, ctx: Context, playbooks: list[Playbook], forced: str | None = None) -> Analysis:
    ctx.question = question
    chosen = select_playbooks(question, playbooks, ctx.store, forced)
    out = Analysis(question, chosen)
    out.related = ctx.store.search(question, limit=5)
    for pb in chosen:
        weights: dict[str, float] = {}
        for check in pb.checks:
            key = f"{pb.id}.{check['id']}"
            out.checks[key] = run_check(check, ctx)
            default = QUESTION_WEIGHT if check["type"] == "question" else 1.0
            weights[check["id"]] = float(check.get("weight", default))
        for cause in pb.causes:
            ids = cause.get("checks", [])
            results = [out.checks[f"{pb.id}.{i}"] for i in ids if f"{pb.id}.{i}" in out.checks]
            found = [r for r in results if r.status == "found"]
            missing = [r for r in results if r.status == "missing"]
            required = [out.checks.get(f"{pb.id}.{i}") for i in cause.get("requires", [])]
            if any(r is not None and r.status == "clear" for r in required):
                out.ruled_out.append((pb, cause))  # a precondition was checked and is false
                continue
            past = ctx.store.confirmed_count(pb.id, cause["id"])
            boost = min(MAX_CASE_BOOST, CASE_BOOST * past)
            base = float(cause.get("base", 0.45))
            if not ids:  # advice with no live check available
                out.causes.append(ScoredCause(pb, cause, min(0.4, base) + boost, [], [], past, advice_only=True))
            elif found:
                score = base + FOUND_WEIGHT * sum(weights[r.check_id] for r in found) + boost
                cap = REPORTED_ONLY_CAP if all(r.reported_only for r in found) else MAX_SCORE
                out.causes.append(ScoredCause(pb, cause, min(cap, score), found, missing, past))
            elif missing:  # could not check: keep, but low and flagged
                out.causes.append(ScoredCause(pb, cause, min(0.3, base / 2) + boost, [], missing, past))
            else:
                out.ruled_out.append((pb, cause))
    out.causes.sort(key=lambda c: c.score, reverse=True)
    out.error_codes = _known_error_codes(question, ctx)
    return out


def _known_error_codes(question: str, ctx: Context) -> list[dict[str, Any]]:
    texts = [("your question", question)]
    try:
        texts += [(h.ref, h.line) for h in ctx.logs.recent_errors(20)]
    except OSError:
        pass
    return ctx.store.match_error_codes(texts)
