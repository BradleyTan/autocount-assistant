"""Scenario playbooks: load them from YAML and check they are well formed.

A playbook lists checks (what to look at) and causes (what a finding means, and the fix).
SQL checks name their tables and columns as candidates, so one playbook works across
AutoCount versions that renamed a table or a field:

    objects:
      tax: {table: [TaxCode, TaxType], columns: {rate: TaxRate, account: TaxAccNo}}
    query: SELECT {tax.rate} FROM {tax} WHERE {tax.account} IS NOT NULL
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .connectors.sqlserver import normalise_objects, placeholders, render_query
from .sql_guard import ReadOnlyViolation, check_read_only

CHECK_TYPES = {"log", "config", "sql", "question", "version_mismatch", "core_dll_mismatch",
               "server_fact", "sql_connect", "windows_service"}
SERVER_FACTS = {"express_used_pct", "log_mb", "data_mb", "log_to_data_ratio", "recovery_model", "status",
                "major_version", "compatibility_level"}
OPERATORS = {">=", ">", "<=", "<", "==", "!="}
_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class PlaybookError(Exception):
    pass


@dataclass
class Playbook:
    id: str
    title: str
    keywords: list[str]
    checks: list[dict[str, Any]]
    causes: list[dict[str, Any]]
    ask_customer: list[str] = field(default_factory=list)
    source: str = "built-in"

    @classmethod
    def from_dict(cls, d: dict[str, Any], source: str = "built-in") -> Playbook:
        return cls(d["id"], d["title"], [str(k).lower() for k in d.get("keywords", [])],
                   list(d.get("checks") or []), list(d.get("causes") or []),
                   [str(q) for q in d.get("ask_customer") or []], source)


def sql_variants(check: dict[str, Any]) -> list[dict[str, Any]]:
    """A sql check is one query, or several `variants` for different table layouts."""
    return list(check.get("variants") or [check])


def _regex_problem(pattern: Any) -> str | None:
    if not isinstance(pattern, str) or not pattern:
        return "is empty"
    try:
        re.compile(pattern)
    except re.error as e:
        return f"is not a valid pattern ({e})"
    return None


def validate_playbook(d: Any) -> list[str]:
    """Problems that would make a playbook misbehave (empty list = valid)."""
    if not isinstance(d, dict):
        return ["file is not a YAML mapping"]
    problems: list[str] = []
    for key in ("id", "title"):
        if not isinstance(d.get(key), str) or not d.get(key):
            problems.append(f"'{key}' is missing")
    if isinstance(d.get("id"), str) and not _ID.match(d["id"]):
        problems.append("'id' must be lower case letters, digits and _")
    if not d.get("keywords"):
        problems.append("'keywords' is empty, so no question can select this playbook")
    check_ids: set[str] = set()
    for i, c in enumerate(d.get("checks") or []):
        if not isinstance(c, dict) or not isinstance(c.get("id"), str):
            problems.append(f"check #{i + 1} has no id")
            continue
        cid = c["id"]
        if cid in check_ids:
            problems.append(f"check '{cid}' is defined twice")
        check_ids.add(cid)
        kind = c.get("type")
        if kind not in CHECK_TYPES:
            problems.append(f"check '{cid}': unknown type '{kind}'")
            continue
        if kind in ("log", "question") and (p := _regex_problem(c.get("pattern"))):
            problems.append(f"check '{cid}': pattern {p}")
        if kind == "config":
            if p := _regex_problem(c.get("key")):
                problems.append(f"check '{cid}': key {p}")
            if c.get("value") is not None and (p := _regex_problem(c.get("value"))):
                problems.append(f"check '{cid}': value {p}")
        if kind == "sql_connect" and c.get("error_pattern") is not None and (
                p := _regex_problem(c.get("error_pattern"))):
            problems.append(f"check '{cid}': error_pattern {p}")
        if kind == "server_fact":
            if c.get("fact") not in SERVER_FACTS:
                problems.append(f"check '{cid}': unknown fact '{c.get('fact')}'")
            if c.get("op") not in OPERATORS or "value" not in c:
                problems.append(f"check '{cid}': needs op (one of {sorted(OPERATORS)}) and value")
        if kind == "sql":
            for n, variant in enumerate(sql_variants(c), start=1):
                problems.extend(f"check '{cid}' variant {n}: {p}" for p in _sql_problems(variant))
    cause_ids: set[str] = set()
    for i, cause in enumerate(d.get("causes") or []):
        if not isinstance(cause, dict) or not isinstance(cause.get("id"), str):
            problems.append(f"cause #{i + 1} has no id")
            continue
        cid = cause["id"]
        if cid in cause_ids:
            problems.append(f"cause '{cid}' is defined twice")
        cause_ids.add(cid)
        for key in ("title", "menu"):
            if not cause.get(key):
                problems.append(f"cause '{cid}': '{key}' is missing")
        if not cause.get("steps"):
            problems.append(f"cause '{cid}': 'steps' is empty")
        for ref in list(cause.get("checks") or []) + list(cause.get("requires") or []):
            if ref not in check_ids:
                problems.append(f"cause '{cid}': refers to unknown check '{ref}'")
        if cause.get("proposed_sql") and not isinstance(cause["proposed_sql"], str):
            problems.append(f"cause '{cid}': proposed_sql must be text")
    if not cause_ids:
        problems.append("no causes defined")
    return problems


def _sql_problems(variant: dict[str, Any]) -> list[str]:
    query = variant.get("query")
    if not isinstance(query, str) or not query.strip():
        return ["query is missing"]
    try:
        objects = normalise_objects(variant.get("objects"))
    except (AttributeError, TypeError):
        return ["objects must look like {alias: {table: Name, columns: {key: Name}}}"]
    known = set(objects) | {f"{a}.{c}" for a, (_, cols) in objects.items() for c in cols}
    unknown = sorted(placeholders(query) - known)
    if unknown:
        return [f"query uses {{{'}, {'.join(unknown)}}} but objects does not define it"]
    try:  # the guard runs again on the real query; this catches playbook mistakes early
        check_read_only(render_query(query, {k: "[x]" for k in known}))
    except ReadOnlyViolation as e:
        return [f"query is not a read-only SELECT ({e})"]
    return []


def load_playbooks(extra_dir: Path | None = None) -> tuple[list[Playbook], list[str]]:
    """(playbooks, problems). Built-in playbooks must be valid; a broken local one is skipped
    and reported. A local playbook with the same id replaces the built-in one."""
    books: dict[str, Playbook] = {}
    problems: list[str] = []
    pkg = resources.files("autocount_assist").joinpath("playbooks")
    for src in sorted((p for p in pkg.iterdir() if p.name.endswith(".yaml")), key=lambda p: p.name):
        data = yaml.safe_load(src.read_text(encoding="utf-8"))
        bad = validate_playbook(data)
        if bad:
            raise PlaybookError(f"Built-in playbook {src.name}: {'; '.join(bad)}")
        books[data["id"]] = Playbook.from_dict(data)
    if extra_dir and extra_dir.is_dir():
        for path in sorted(extra_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
                problems.append(f"{path.name}: cannot be read ({type(e).__name__})")
                continue
            bad = validate_playbook(data)
            if bad:
                problems.append(f"{path.name}: skipped: {'; '.join(bad)}")
                continue
            books[data["id"]] = Playbook.from_dict(data, source=str(path))
    return list(books.values()), problems
