"""Match a question to playbooks, run their checks against the live environment, score causes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .config import Settings
from .connectors.config_files import ConfigConnector
from .connectors.logs import LogConnector
from .connectors.plugins import PluginConnector
from .connectors.sqlserver import SqlServerConnector, SqlUnavailable
from .connectors.version import VersionConnector, VersionReport
from .knowledge.store import KnowledgeStore
from .masking import Masker
from .models import CheckResult, Evidence
from .sql_guard import ReadOnlyViolation

FOUND_WEIGHT = 0.2
CASE_BOOST = 0.05
MAX_CASE_BOOST = 0.15
MAX_SCORE = 0.95


@dataclass
class Playbook:
    id: str
    title: str
    keywords: list[str]
    checks: list[dict[str, Any]]
    causes: list[dict[str, Any]]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Playbook":
        return cls(d["id"], d["title"], [k.lower() for k in d.get("keywords", [])],
                   d.get("checks", []), d.get("causes", []))


def load_playbooks(extra_dir: Path | None = None) -> list[Playbook]:
    books: dict[str, Playbook] = {}
    pkg = resources.files("autocount_assist").joinpath("playbooks")
    sources = [p for p in pkg.iterdir() if p.name.endswith(".yaml")]
    if extra_dir and extra_dir.is_dir():  # local overrides / additions, same id replaces
        sources += sorted(extra_dir.glob("*.yaml"))
    for src in sources:
        pb = Playbook.from_dict(yaml.safe_load(src.read_text()))
        books[pb.id] = pb
    return list(books.values())


@dataclass
class Context:
    settings: Settings
    masker: Masker
    logs: LogConnector
    config: ConfigConnector
    plugins: PluginConnector
    sql: SqlServerConnector | None
    store: KnowledgeStore
    _version: VersionReport | None = None
    sql_error: str | None = None

    @property
    def version(self) -> VersionReport:
        if self._version is None:
            self._version = VersionConnector(self.settings, self.plugins, self.sql).check()
        return self._version


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


def select_playbooks(question: str, playbooks: list[Playbook], store: KnowledgeStore,
                     forced: str | None = None, top: int = 2) -> list[Playbook]:
    if forced:
        return [p for p in playbooks if p.id == forced]
    q = question.lower()
    scores: dict[str, float] = {}
    for p in playbooks:
        s = sum(1.0 for k in p.keywords if re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", q))
        if s:
            scores[p.id] = s
    for hit in store.search(question, limit=10, kinds=("case",)):  # past confirmed cases vote too
        case = store.case(int(hit["ref"]))
        if case and case["playbook_id"]:
            scores[case["playbook_id"]] = scores.get(case["playbook_id"], 0) + 0.5
    ranked = sorted(scores, key=lambda pid: scores[pid], reverse=True)
    best = scores[ranked[0]] if ranked else 0
    return [p for p in playbooks if p.id in ranked[:top] and scores[p.id] >= max(1.0, best / 2)]


# ---- checks -------------------------------------------------------------

def run_check(check: dict[str, Any], ctx: Context) -> CheckResult:
    kind = check["type"]
    res = CheckResult(check["id"], "missing", check.get("describe", check["id"]),
                      verified=check.get("verified", True))
    try:
        if kind == "log":
            if not ctx.logs.discover():
                res.note = "No AutoCount log files found in the configured log folders"
                return res
            hits = ctx.logs.search(check["pattern"], limit=check.get("limit", 5))
            res.status = "found" if hits else "clear"
            res.evidence = [Evidence("log", h.ref, h.line) for h in hits]
            res.note = f"Searched {len(ctx.logs.discover())} log file(s)"
        elif kind == "config":
            if not ctx.config.entries():
                res.note = "No readable config files found"
                return res
            keyed = ctx.config.find(check["key"])
            if check.get("value"):
                if not keyed:  # the setting isn't in any file we can read: unknown, not "clear"
                    res.note = "Setting not found in the config files read"
                    return res
                hits = ctx.config.find(check["key"], check["value"])
                res.status = "found" if hits else "clear"
            else:
                hits = keyed
                want_present = check.get("finding_when", "present") == "present"
                res.status = "found" if bool(hits) == want_present else "clear"
            res.evidence = [Evidence("config", f"{e.file} :: {e.key}", e.value) for e in hits[:5]]
            if res.status == "found" and not hits:
                res.evidence = [Evidence("config", check["key"], "setting not present in any config file")]
        elif kind == "sql":
            if ctx.sql is None:
                res.note = ctx.sql_error or "No account book database selected"
                return res
            missing = ctx.sql.missing_objects(check.get("requires", {}))
            if missing:
                res.note = f"Not found in this database: {', '.join(missing)} (AutoCount table name unverified)"
                return res
            rows = ctx.sql.query(check["query"])
            when = check.get("finding_when", "rows")
            if when.startswith("nonzero:"):
                col = when.split(":", 1)[1]
                found = bool(rows) and any((r.get(col) or 0) > 0 for r in rows)
            else:
                found = bool(rows)
            res.status = "found" if found else "clear"
            ref = check.get("ref", "query")
            res.evidence = [Evidence("sql", ref, _fmt_row(r)) for r in rows[:5]] if found else []
            res.note = f"Ran read-only query on {ctx.sql.database}"
        elif kind == "version_mismatch":
            v = ctx.version
            if v.status == "unknown":
                res.note = "; ".join(v.notes) or "Version unknown"
                return res
            want = check.get("direction", "any")
            found = v.status == "mismatch" and (want == "any" or v.direction == want)
            res.status = "found" if found else "clear"
            res.evidence = [Evidence("version", "system vs database",
                                     f"system {v.app_version} ({v.app_source}); database {v.db_version} "
                                     f"({v.db_source}); expected database {v.expected_db}")]
        elif kind == "core_dll_mismatch":
            app = ctx.version.app_version
            if not app:
                res.note = "System version unknown, cannot compare DLL versions"
                return res
            if not ctx.plugins.list_binaries():
                res.note = "No DLLs found under the configured plugin folders"
                return res
            bad = ctx.plugins.core_version_mismatches(app)
            res.status = "found" if bad else "clear"
            res.evidence = [Evidence("plugin", b.path, f"file version {b.file_version}, system is {app}")
                            for b in bad[:5]]
        else:
            res.note = f"Unknown check type '{kind}'"
    except SqlUnavailable as e:
        ctx.sql_error = str(e)
        ctx.sql = None
        res.status, res.note = "missing", str(e)
    except ReadOnlyViolation as e:  # a playbook bug: never run, report it
        res.status, res.note = "missing", f"Blocked by read-only guard: {e}"
    except Exception as e:  # one broken check must not sink the whole answer
        res.status, res.note = "missing", f"Check failed: {type(e).__name__}: {e}"
    return res


def _fmt_row(row: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in row.items())


# ---- scoring ------------------------------------------------------------

def analyse(question: str, ctx: Context, playbooks: list[Playbook], forced: str | None = None) -> Analysis:
    chosen = select_playbooks(question, playbooks, ctx.store, forced)
    out = Analysis(question, chosen)
    out.related = ctx.store.search(question, limit=5)
    for pb in chosen:
        for check in pb.checks:
            key = f"{pb.id}.{check['id']}"
            out.checks[key] = run_check(check, ctx)
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
                score = min(MAX_SCORE, base + FOUND_WEIGHT * len(found) + boost)
                out.causes.append(ScoredCause(pb, cause, score, found, missing, past))
            elif missing:  # could not check: keep, but low and flagged
                out.causes.append(ScoredCause(pb, cause, min(0.3, base / 2) + boost, [], missing, past))
            else:
                out.ruled_out.append((pb, cause))
    out.causes.sort(key=lambda c: c.score, reverse=True)
    return out
