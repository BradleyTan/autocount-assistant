"""autocount-assist command line.

ask / scan / dbs / cases / demo only read and print. Commands that save anything
(learn ..., --save-proposal, confirming a case) ask first, or need --yes.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .analyser import Context, analyse, load_playbooks
from .config import ConfigError, Settings, ensure_safe_data_dir, load_settings
from .connectors.config_files import ConfigConnector
from .connectors.logs import LogConnector
from .connectors.plugins import PluginConnector
from .connectors.sqlserver import SqlServerConnector, SqlUnavailable
from .knowledge.release_notes import fetch_release_notes
from .knowledge.schema_learner import learn_schema
from .knowledge.store import KnowledgeStore
from .masking import Masker
from .recommender import Report, build_report, render_text


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def confirm(question: str, yes: bool) -> bool:
    """Ask before saving anything. --yes counts as agreement; non-interactive without --yes is a no."""
    if yes:
        return True
    if not _interactive():
        print(f"Not saved: {question} Re-run with --yes to agree.", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def make_context(settings: Settings, database: str | None, allow_write: bool = False,
                 connect: Callable[[str], Any] | None = None, check_location: bool = True,
                 sql_error: str | None = None) -> Context:
    masker = Masker()
    sql = SqlServerConnector(settings, masker, database, connect=connect) if database else None
    store = KnowledgeStore(settings.kb_path, masker, allow_write=allow_write, check_location=check_location)
    return Context(
        settings=settings, masker=masker,
        logs=LogConnector(settings.expanded(settings.log_dirs), masker),
        config=ConfigConnector(settings.expanded(settings.config_dirs), masker),
        plugins=PluginConnector(settings.expanded(settings.plugin_dirs)),
        sql=sql, store=store, sql_error=sql_error)


def resolve_database(args, settings: Settings, connect=None) -> tuple[str | None, str | None]:
    """Returns (database, reason_if_none). Picks interactively when possible."""
    if getattr(args, "no_db", False):
        return None, "database checks turned off (--no-db)"
    if getattr(args, "db", None):
        return args.db, None
    if settings.database:
        return settings.database, None
    try:
        dbs = SqlServerConnector(settings, Masker(), None, connect=connect).list_databases()
    except SqlUnavailable as e:
        return None, str(e)
    books = [d["name"] for d in dbs if d["looks_like_account_book"]] or [d["name"] for d in dbs]
    if not books:
        return None, f"no databases found on {settings.sql_server}"
    if not _interactive():
        return None, "no account book selected; pass --db (run `autocount-assist dbs` to list them)"
    print("Account books on", settings.sql_server)
    for i, name in enumerate(books, start=1):
        print(f"  {i}. {name}")
    choice = input("Pick an account book number (Enter to skip database checks): ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(books):
        return books[int(choice) - 1], None
    return None, "no account book selected"


# ---- commands -----------------------------------------------------------

def cmd_ask(args, settings: Settings, connect=None, check_location=True) -> int:
    db, why = resolve_database(args, settings, connect)
    ctx = make_context(settings, db, connect=connect, check_location=check_location, sql_error=why)
    playbooks = load_playbooks(settings.data_dir / "playbooks")
    report = build_report(analyse(args.question, ctx, playbooks, forced=args.playbook), ctx)
    print(report.to_json() if args.json else render_text(report))
    if args.save_proposal:
        _save_proposals(report, settings, args.yes)
    if not args.json and _interactive() and report.recommendations:
        _offer_case_capture(report, settings, ctx)
    if ctx.sql:
        ctx.sql.close()
    return 0


def _save_proposals(report: Report, settings: Settings, yes: bool) -> None:
    props = [r for r in report.recommendations if r.proposed_change]
    if not props:
        print("No data-fix scripts were proposed for this question.")
        return
    if not confirm(f"Save {len(props)} proposed SQL script(s) to {settings.proposed_dir}?", yes):
        return
    ensure_safe_data_dir(settings.proposed_dir)
    settings.proposed_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for r in props:
        path = settings.proposed_dir / f"{stamp}_{r.playbook}_{r.cause_id}.sql"
        path.write_text(
            "-- PROPOSED CHANGE. NOT EXECUTED by autocount-assist.\n"
            f"-- Question: {report.question}\n-- Cause: {r.cause}\n"
            "-- Review, back up the account book, and run it yourself only if you approve.\n\n"
            + r.proposed_change + "\n", encoding="utf-8")
        print(f"Saved {path}")


def _offer_case_capture(report: Report, settings: Settings, ctx: Context) -> None:
    ans = input("Did one of these solve it? Enter its number to record the case, or press Enter to skip: ").strip()
    if not (ans.isdigit() and 1 <= int(ans) <= len(report.recommendations)):
        return
    rec = report.recommendations[int(ans) - 1]
    fix = input("What fixed it (Enter to use the listed steps)? ").strip() or " / ".join(rec.steps)
    if not confirm(f"Save this case (masked) to {settings.kb_path}?", False):
        return
    store = KnowledgeStore(settings.kb_path, ctx.masker, allow_write=True)
    case_id = store.add_case(report.question, rec.cause, fix, rec.playbook, rec.cause_id,
                             [f"{e['ref']}: {e['detail']}" for e in rec.evidence],
                             report.environment["system_version"] or "", report.environment["database_version"] or "")
    print(f"Saved case #{case_id}.")


def cmd_scan(args, settings: Settings, connect=None, check_location=True) -> int:
    db, why = resolve_database(args, settings, connect)
    ctx = make_context(settings, db, connect=connect, check_location=check_location, sql_error=why)
    v = ctx.version
    binaries = ctx.plugins.list_binaries()
    core_mismatch = ctx.plugins.core_version_mismatches(v.app_version) if v.app_version else []
    out = {
        "settings": {"install_path": settings.install_path, "sql_server": settings.sql_server,
                     "account_book": db, "data_dir": str(settings.data_dir)},
        "versions": {"system": v.app_version, "system_source": v.app_source, "database": v.db_version,
                     "database_source": v.db_source, "expected_database": v.expected_db,
                     "compatibility": v.status, "direction": v.direction, "notes": v.notes},
        "log_files": [str(p) for p in ctx.logs.discover()[:20]],
        "recent_errors": [{"ref": h.ref, "line": h.line} for h in ctx.logs.recent_errors(10)],
        "config_files": sorted({e.file for e in ctx.config.entries()}),
        "binaries": [{"name": b.name, "file_version": b.file_version, "path": b.path} for b in binaries],
        "core_dll_mismatches": [b.name for b in core_mismatch],
        "database": None,
        "knowledge_base": ctx.store.stats(),
    }
    if ctx.sql:
        try:
            out["database"] = {"tables": len(ctx.sql.columns()),
                               "audit_tables": ctx.sql.tables_like("%Audit%"),
                               "version_tables": ctx.sql.tables_like("%Version%")}
        except SqlUnavailable as e:
            out["database"] = {"error": str(e)}
    elif why:
        out["database"] = {"skipped": why}
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        _print_scan(out)
    return 0


def _print_scan(o: dict[str, Any]) -> None:
    v = o["versions"]
    print(f"Install path : {o['settings']['install_path']}")
    print(f"SQL Server   : {o['settings']['sql_server']}  account book: {o['settings']['account_book'] or '(none)'}")
    print(f"System ver.  : {v['system'] or 'unknown'}  ({v['system_source']})")
    print(f"Database ver.: {v['database'] or 'unknown'}  ({v['database_source']})")
    print(f"Expected DB  : {v['expected_database'] or 'unknown'}  -> {v['compatibility'].upper()}"
          + (f" ({v['direction']})" if v["direction"] else ""))
    for n in v["notes"]:
        print(f"  ! {n}")
    print(f"Log files    : {len(o['log_files'])} found")
    for e in o["recent_errors"]:
        print(f"  - {e['ref']}\n      {e['line']}")
    print(f"Config files : {len(o['config_files'])}")
    print(f"DLL/EXE      : {len(o['binaries'])}; AutoCount DLL version mismatches: "
          f"{', '.join(o['core_dll_mismatches']) or 'none'}")
    print(f"Database     : {o['database']}")
    print(f"Knowledge base: {o['knowledge_base']}")


def cmd_dbs(args, settings: Settings, connect=None, **_) -> int:
    try:
        dbs = SqlServerConnector(settings, Masker(), None, connect=connect).list_databases()
    except SqlUnavailable as e:
        print(f"Cannot list databases: {e}", file=sys.stderr)
        return 2
    for d in dbs:
        print(f"{'*' if d['looks_like_account_book'] else ' '} {d['name']}  ({d['state_desc']})")
    print("* = looks like an AutoCount account book")
    return 0


def _writable_store(settings: Settings, check_location: bool) -> KnowledgeStore:
    return KnowledgeStore(settings.kb_path, Masker(), allow_write=True, check_location=check_location)


def cmd_learn(args, settings: Settings, connect=None, check_location=True) -> int:
    what = args.what
    if what == "schema":
        db, why = resolve_database(args, settings, connect)
        if not db:
            print(f"Cannot learn schema: {why}", file=sys.stderr)
            return 2
        if not confirm(f"Save the table/column structure of {db} (no row data) to {settings.kb_path}?", args.yes):
            return 1
        store = _writable_store(settings, check_location)
        sql = SqlServerConnector(settings, store.masker, db, connect=connect)
        try:
            tables, fks = learn_schema(sql, store, db)
        except SqlUnavailable as e:
            print(f"Cannot learn schema: {e}", file=sys.stderr)
            return 2
        print(f"Saved {tables} tables and {fks} relationships from {db}.")
    elif what == "release-notes":
        if not confirm(f"Download AutoCount 2.2 release notes from wiki.autocountsoft.com and save them to {settings.kb_path}?", args.yes):
            return 1
        store = _writable_store(settings, check_location)
        n = 0
        try:
            for note in fetch_release_notes(limit=args.limit):
                store.add_release_note(**note)
                n += 1
                print(f"  saved {note['version']}")
        except OSError as e:
            print(f"Download stopped: {e}", file=sys.stderr)
        print(f"Saved {n} release note(s).")
    elif what == "case":
        if not confirm(f"Save this solved case (masked) to {settings.kb_path}?", args.yes):
            return 1
        store = _writable_store(settings, check_location)
        case_id = store.add_case(args.scenario, args.cause, args.fix, args.playbook or "", args.cause_id or "",
                                 args.evidence or [])
        print(f"Saved case #{case_id}.")
    elif what == "error-code":
        if not confirm(f"Save error code {args.code} to {settings.kb_path}?", args.yes):
            return 1
        _writable_store(settings, check_location).add_error_code(
            args.code, args.source, args.message, args.cause, args.fix)
        print(f"Saved error code {args.code}.")
    return 0


def cmd_cases(args, settings: Settings, **_) -> int:
    store = KnowledgeStore(settings.kb_path, Masker())
    rows = store.cases(args.playbook)
    if args.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("No confirmed cases yet.")
    for r in [] if args.json else rows:
        print(f"#{r['id']} {r['created_at']} [{r['playbook_id']}/{r['cause_id']}]\n  Scenario: {r['scenario']}\n"
              f"  Cause: {r['cause']}\n  Fix: {r['fix']}")
    return 0


def cmd_demo(args, settings: Settings, **_) -> int:
    from .demo import build_env, scenario_ids

    ids = scenario_ids() if args.scenario == "all" else [args.scenario]
    for sid in ids:
        if sid not in scenario_ids():
            print(f"Unknown scenario '{sid}'. Choose from: {', '.join(scenario_ids())}", file=sys.stderr)
            return 2
        with tempfile.TemporaryDirectory() as tmp:
            env = build_env(sid, Path(tmp))
            env.settings.app_version = None
            ctx = make_context(env.settings, "AED_DEMO", connect=lambda _s, c=env.connection: c,
                               check_location=False)
            report = build_report(analyse(env.spec["question"], ctx, load_playbooks()), ctx)
            print(report.to_json() if args.json else f"=== Demo: {sid} (synthetic data) ===\n" + render_text(report))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autocount-assist", description="Read-only AutoCount desktop support assistant")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", help="Path to autocount-assist.toml")
    sub = p.add_subparsers(dest="command", required=True)

    def db_opts(sp):
        sp.add_argument("--db", help="Account book database name (e.g. AED_ACME)")
        sp.add_argument("--no-db", action="store_true", help="Skip database checks")

    a = sub.add_parser("ask", help="Analyse a question or customer scenario")
    a.add_argument("question")
    db_opts(a)
    a.add_argument("--playbook", help="Force a specific playbook id")
    a.add_argument("--json", action="store_true")
    a.add_argument("--save-proposal", action="store_true", help="Save proposed SQL fix scripts (asks first)")
    a.add_argument("--yes", action="store_true", help="Agree to save without prompting")

    s = sub.add_parser("scan", help="Summarise the AutoCount environment")
    db_opts(s)
    s.add_argument("--json", action="store_true")

    sub.add_parser("dbs", help="List databases on the SQL Server instance")

    lr = sub.add_parser("learn", help="Add to the local knowledge base (asks before saving)")
    lsub = lr.add_subparsers(dest="what", required=True)
    ls = lsub.add_parser("schema")
    db_opts(ls)
    ls.add_argument("--yes", action="store_true")
    ln = lsub.add_parser("release-notes")
    ln.add_argument("--limit", type=int, default=None, help="Only the newest N versions")
    ln.add_argument("--yes", action="store_true")
    lc = lsub.add_parser("case")
    lc.add_argument("--scenario", required=True)
    lc.add_argument("--cause", required=True)
    lc.add_argument("--fix", required=True)
    lc.add_argument("--playbook")
    lc.add_argument("--cause-id")
    lc.add_argument("--evidence", action="append")
    lc.add_argument("--yes", action="store_true")
    le = lsub.add_parser("error-code")
    le.add_argument("--code", required=True)
    le.add_argument("--message", required=True)
    le.add_argument("--cause", required=True)
    le.add_argument("--fix", required=True)
    le.add_argument("--source", default="AutoCount")
    le.add_argument("--yes", action="store_true")

    c = sub.add_parser("cases", help="List confirmed cases")
    c.add_argument("--playbook")
    c.add_argument("--json", action="store_true")

    d = sub.add_parser("demo", help="Run a sample scenario on synthetic data")
    d.add_argument("scenario", help="Scenario id or 'all'")
    d.add_argument("--json", action="store_true")
    return p


COMMANDS = {"ask": cmd_ask, "scan": cmd_scan, "dbs": cmd_dbs, "learn": cmd_learn,
            "cases": cmd_cases, "demo": cmd_demo}


def main(argv: list[str] | None = None, connect=None, check_location: bool = True) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        return COMMANDS[args.command](args, settings, connect=connect, check_location=check_location)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
