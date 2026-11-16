"""autocount-assist command line.

Read-only commands (ask, scan, dbs, doctor, query, logs, config, schema, check-playbooks,
cases, demo) never save anything. Commands that save (learn, propose, setup-login) ask
first, or need --yes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__, doctor, setup_login
from .analyser import Context, analyse, since_from_days
from .config import ConfigError, Settings, ensure_safe_data_dir, load_settings
from .connectors import system
from .connectors.config_files import ConfigConnector
from .connectors.logs import LogConnector
from .connectors.plugins import PluginConnector
from .connectors.sqlserver import SqlServerConnector, SqlUnavailable
from .knowledge.release_notes import fetch_release_notes
from .knowledge.schema_learner import learn_schema
from .knowledge.store import KnowledgeStore
from .masking import Masker
from .playbook import PlaybookError, load_playbooks, sql_variants
from .recommender import Report, build_report, render_text, render_ticket_note
from .sql_guard import ReadOnlyViolation

Connect = Callable[[str], Any]
_SINCE = re.compile(r"^(\d+)\s*([dh])$")
_SCAN_TABLE_PATTERNS = {"option_tables": ["%Option%", "%Setting%", "%Registry%"],
                        "audit_tables": ["%Audit%", "%Log"], "version_tables": ["%Version%"],
                        "user_tables": ["Users", "%UserGroup%", "%AccessRight%"]}


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


def parse_since(value: str | None, settings: Settings, now: dt.datetime | None = None) -> dt.datetime | None:
    """'7d', '12h', '2026-11-01' or 'all'. Without a value, the log_days setting applies."""
    now = now or dt.datetime.now()
    if value is None:
        return since_from_days(settings.log_days, now)
    text = value.strip().lower()
    if text in ("all", "0"):
        return None
    if m := _SINCE.match(text):
        amount = int(m.group(1))
        return now - (dt.timedelta(days=amount) if m.group(2) == "d" else dt.timedelta(hours=amount))
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        raise ConfigError(f"--since '{value}' is not understood. Use 7d, 12h, 2026-11-01 or all.") from None


def make_context(settings: Settings, database: str | None, allow_write: bool = False,
                 connect: Connect | None = None, check_location: bool = True,
                 sql_error: str | None = None, since: dt.datetime | None | str = "default",
                 service_state: Callable[[str], str] | None = None) -> Context:
    masker = Masker()
    sql = SqlServerConnector(settings, masker, database, connect=connect) if database else None
    store = KnowledgeStore(settings.kb_path, masker, allow_write=allow_write, check_location=check_location)
    window = since_from_days(settings.log_days) if isinstance(since, str) else since
    return Context(
        settings=settings, masker=masker,
        logs=LogConnector(settings.expanded(settings.log_dirs), masker, since=window),
        config=ConfigConnector(settings.expanded(settings.config_dirs), masker),
        plugins=PluginConnector(settings.expanded(settings.plugin_dirs)),
        sql=sql, store=store, sql_error=sql_error,
        server=SqlServerConnector(settings, masker, None, connect=connect),
        service_state=service_state or system.service_state)


def resolve_database(args: argparse.Namespace, settings: Settings,
                     connect: Connect | None = None) -> tuple[str | None, str | None]:
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


def _close(ctx: Context) -> None:
    for conn in (ctx.sql, ctx.server):
        if conn:
            conn.close()


def _run_analysis(args: argparse.Namespace, settings: Settings, connect: Connect | None,
                  check_location: bool) -> tuple[Report, Context]:
    db, why = resolve_database(args, settings, connect)
    ctx = make_context(settings, db, connect=connect, check_location=check_location, sql_error=why,
                       since=parse_since(getattr(args, "since", None), settings))
    playbooks, problems = load_playbooks(settings.playbook_dir)
    report = build_report(analyse(args.question, ctx, playbooks, forced=args.playbook), ctx,
                          playbook_problems=[f"Local playbook {p}" for p in problems])
    return report, ctx


# ---- commands -----------------------------------------------------------

def cmd_ask(args: argparse.Namespace, settings: Settings, connect: Connect | None = None,
            check_location: bool = True) -> int:
    report, ctx = _run_analysis(args, settings, connect, check_location)
    try:
        if args.json:
            print(report.to_json())
        else:
            print(render_text(report), end="")
            if args.ticket_note:
                print("\n--- Ticket note (masked) ---\n" + render_ticket_note(report), end="")
            if _interactive() and report.recommendations:
                _offer_case_capture(report, settings, ctx)
    finally:
        _close(ctx)
    return 0


def cmd_propose(args: argparse.Namespace, settings: Settings, connect: Connect | None = None,
                check_location: bool = True) -> int:
    """Save proposed data-fix scripts for review. Nothing is ever run."""
    report, ctx = _run_analysis(args, settings, connect, check_location)
    _close(ctx)
    props = [r for r in report.recommendations if r.proposed_change]
    if not props:
        print("No data-fix scripts are proposed for this question. The fixes are the steps shown by `ask`.")
        return 0
    if not confirm(f"Save {len(props)} proposed SQL script(s) to {settings.proposed_dir}?", args.yes):
        return 1
    if check_location:
        ensure_safe_data_dir(settings.proposed_dir)
    settings.proposed_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    for r in props:
        path = settings.proposed_dir / f"{stamp}_{r.playbook}_{r.cause_id}.sql"
        path.write_text(
            "-- PROPOSED CHANGE. NOT EXECUTED by autocount-assist.\n"
            f"-- Question: {report.question}\n-- Cause: {r.cause}\n"
            "-- Review, back up the account book, and run it yourself only if you approve.\n\n"
            + (r.proposed_change or "") + "\n", encoding="utf-8")
        print(f"Saved {path}")
    return 0


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


def _scan_database(sql: SqlServerConnector) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        login = sql.login_info()
        out["login"] = {"login_name": login.get("login_name"), "read_only": not login["can_write"],
                        "write_rights": login["write_rights"]}
        out["sql_server"] = sql.server_info()
        out["tables"] = len(sql.catalog())
        out["largest_tables"] = sql.row_counts(15)
        for key, patterns in _SCAN_TABLE_PATTERNS.items():
            found: list[str] = []
            for pattern in patterns:
                found += [t for t in sql.tables_like(pattern) if t not in found]
            out[key] = found[:20]
        latest = {}
        for table in out["audit_tables"][:5]:
            stamp = sql.latest_timestamp(table)
            if stamp:
                latest[table] = {"column": stamp[0], "latest": stamp[1]}
        out["audit_latest"] = latest
    except SqlUnavailable as e:
        out["error"] = str(e)
    return out


def cmd_scan(args: argparse.Namespace, settings: Settings, connect: Connect | None = None,
             check_location: bool = True) -> int:
    db, why = resolve_database(args, settings, connect)
    ctx = make_context(settings, db, connect=connect, check_location=check_location, sql_error=why,
                       since=parse_since(args.since, settings))
    v = ctx.version
    binaries = ctx.plugins.list_binaries()
    core_mismatch = ctx.plugins.core_version_mismatches(v.app_version) if v.app_version else []
    mp = ctx.masker.mask_path
    out: dict[str, Any] = {
        "tool_version": __version__,
        "settings": {"install_path": mp(settings.install_path), "sql_server": settings.sql_server,
                     "account_book": db, "data_dir": mp(str(settings.data_dir)), "log_window": ctx.log_window},
        "versions": {"system": v.app_version, "system_source": mp(v.app_source), "database": v.db_version,
                     "database_source": v.db_source, "expected_database": v.expected_db,
                     "compatibility": v.status, "direction": v.direction, "notes": v.notes},
        "log_files": [ctx.logs.display_path(p) for p in ctx.logs.discover()[:20]],
        "recent_errors": [{"ref": h.ref, "time": h.timestamp, "line": h.line, "context": h.context}
                          for h in ctx.logs.recent_errors(10, context=3)],
        "config_files": sorted({mp(e.file) for e in ctx.config.entries()}),
        "binaries": [{"name": b.name, "file_version": b.file_version, "path": mp(b.path)} for b in binaries],
        "core_dll_mismatches": [b.name for b in core_mismatch],
        "database": None,
        "knowledge_base": ctx.store.stats(),
    }
    if ctx.sql:
        out["database"] = _scan_database(ctx.sql)
    elif why:
        out["database"] = {"skipped": why}
    _close(ctx)
    if args.json:
        if not args.binaries:
            out["binaries"] = [b for b in out["binaries"] if b["name"].lower().startswith("autocount")]
        print(json.dumps(out, indent=2, default=str))
    else:
        _print_scan(out, args.binaries)
    return 0


def _print_scan(o: dict[str, Any], show_binaries: bool) -> None:
    v = o["versions"]
    print(f"autocount-assist {o['tool_version']}")
    print(f"Install path : {o['settings']['install_path']}")
    print(f"SQL Server   : {o['settings']['sql_server']}  account book: {o['settings']['account_book'] or '(none)'}")
    print(f"System ver.  : {v['system'] or 'unknown'}  ({v['system_source']})")
    print(f"Database ver.: {v['database'] or 'unknown'}  ({v['database_source']})")
    print(f"Expected DB  : {v['expected_database'] or 'unknown'}  -> {v['compatibility'].upper()}"
          + (f" ({v['direction']})" if v["direction"] else ""))
    for n in v["notes"]:
        print(f"  ! {n}")
    print(f"Log files    : {len(o['log_files'])} found; errors {o['settings']['log_window']}: {len(o['recent_errors'])}")
    for e in o["recent_errors"]:
        print(f"  - {e['ref']}\n      {e['line']}")
        for extra in e["context"]:
            print(f"        {extra}")
    print(f"Config files : {len(o['config_files'])}")
    print(f"DLL/EXE      : {len(o['binaries'])}; AutoCount DLL version mismatches: "
          f"{', '.join(o['core_dll_mismatches']) or 'none'}")
    if show_binaries:
        for b in o["binaries"]:
            print(f"  - {b['name']}  {b['file_version'] or '?'}")
    d = o["database"]
    if not d:
        print("Database     : (none)")
    elif "skipped" in d or "error" in d and "sql_server" not in d:
        print(f"Database     : not read: {d.get('skipped') or d.get('error')}")
    else:
        s = d["sql_server"]
        login = d["login"]
        print(f"Database     : {s.get('database_name')} is {s.get('status')}, {d.get('tables', '?')} tables, "
              f"data {s.get('data_mb')} MB, log {s.get('log_mb')} MB, recovery {s.get('recovery_model')}")
        print(f"SQL edition  : SQL Server {s.get('release') or s.get('product_version')} {s.get('edition')}"
              + (f"; {s['express_used_pct']}% of the 10 GB Express limit" if s.get("is_express") else ""))
        print(f"Login        : {login['login_name']} "
              + ("(read-only)" if login["read_only"] else f"CAN CHANGE DATA ({', '.join(login['write_rights'])})"))
        for label, key in (("Option tables", "option_tables"), ("Audit tables", "audit_tables"),
                           ("Version tables", "version_tables"), ("User tables", "user_tables")):
            if d.get(key):
                print(f"{label:<13}: {', '.join(d[key])}")
        for table, stamp in (d.get("audit_latest") or {}).items():
            print(f"  latest {table}.{stamp['column']}: {stamp['latest']}")
        if d.get("largest_tables"):
            print("Largest tables: " + ", ".join(f"{r['table_name']} ({r['row_count']})"
                                                 for r in d["largest_tables"][:8]))
        if d.get("error"):
            print(f"  ! {d['error']}")
    print(f"Knowledge base: {o['knowledge_base']}")


def cmd_dbs(args: argparse.Namespace, settings: Settings, connect: Connect | None = None, **_: Any) -> int:
    try:
        dbs = SqlServerConnector(settings, Masker(), None, connect=connect).list_databases()
    except SqlUnavailable as e:
        print(f"Cannot list databases: {e}", file=sys.stderr)
        return 2
    for d in dbs:
        print(f"{'*' if d['looks_like_account_book'] else ' '} {d['name']}  ({d['state_desc']})")
    print("* = looks like an AutoCount account book")
    return 0


def cmd_doctor(args: argparse.Namespace, settings: Settings, connect: Connect | None = None, **_: Any) -> int:
    findings = doctor.run_doctor(settings, args.db, connect=connect)
    if args.json:
        print(json.dumps([f.to_dict() for f in findings], indent=2))
    else:
        print(doctor.render(findings), end="")
    return 1 if any(f.status == doctor.FAIL for f in findings) else 0


def _print_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("(no rows)")
        return
    cols = list(rows[0])
    text = [[("" if r.get(c) is None else str(r.get(c)))[:60] for c in cols] for r in rows]
    widths = [max(len(c), *(len(t[i]) for t in text)) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths, strict=True)))
    print("  ".join("-" * w for w in widths))
    for t in text:
        print("  ".join(v.ljust(w) for v, w in zip(t, widths, strict=True)))
    print(f"({len(rows)} row(s), customer data masked)")


def _need_db(args: argparse.Namespace, settings: Settings, connect: Connect | None) -> str | None:
    db, why = resolve_database(args, settings, connect)
    if not db:
        print(f"No account book: {why}", file=sys.stderr)
    return db


def cmd_query(args: argparse.Namespace, settings: Settings, connect: Connect | None = None, **_: Any) -> int:
    """Run one read-only SELECT. The guard, the login check and masking all apply."""
    db = _need_db(args, settings, connect)
    if not db:
        return 2
    sql = SqlServerConnector(settings, Masker(), db, connect=connect)
    try:
        rows = sql.query(args.sql, limit=max(1, min(args.limit, 1000)))
    except ReadOnlyViolation as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 2
    except SqlUnavailable as e:
        print(f"Query failed: {e}", file=sys.stderr)
        return 2
    finally:
        sql.close()
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        _print_rows(rows)
    return 0


def cmd_logs(args: argparse.Namespace, settings: Settings, **_: Any) -> int:
    logs = LogConnector(settings.expanded(settings.log_dirs), Masker(), since=parse_since(args.since, settings))
    try:
        found = logs.find(args.pattern or r"\b(exception|error|failed|fatal)\b", limit=args.limit, context=args.context)
    except re.error as e:
        print(f"Pattern is not valid: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"files_searched": found.files_searched, "older_ignored": found.older_ignored,
                          "hits": [{"ref": h.ref, "time": h.timestamp, "line": h.line, "context": h.context}
                                   for h in found.hits]}, indent=2))
        return 0
    for h in found.hits:
        print(f"{h.ref}\n    {h.line}")
        for extra in h.context:
            print(f"      {extra}")
    print(f"{len(found.hits)} match(es) in {found.files_searched} log file(s) searched"
          + (f"; {found.older_ignored} older match(es) ignored (use --since all)" if found.older_ignored else ""))
    return 0


def cmd_config(args: argparse.Namespace, settings: Settings, **_: Any) -> int:
    masker = Masker()
    conn = ConfigConnector(settings.expanded(settings.config_dirs), masker)
    try:
        entries = conn.find(args.key or "")
    except re.error as e:
        print(f"Pattern is not valid: {e}", file=sys.stderr)
        return 2
    rows = [{"file": masker.mask_path(e.file), "key": e.key, "value": e.value} for e in entries[:args.limit]]
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['file']} :: {r['key']} = {r['value']}")
        print(f"{len(rows)} of {len(entries)} setting(s); secrets masked")
    return 0


def cmd_schema(args: argparse.Namespace, settings: Settings, connect: Connect | None = None, **_: Any) -> int:
    db = _need_db(args, settings, connect)
    if not db:
        return 2
    sql = SqlServerConnector(settings, Masker(), db, connect=connect)
    try:
        catalog = sql.catalog()
    except SqlUnavailable as e:
        print(f"Cannot read the table list: {e}", file=sys.stderr)
        return 2
    finally:
        sql.close()
    needle = (args.pattern or "").lower()
    tables = {name: cols for _, (name, cols) in sorted(catalog.items())
              if needle in name.lower() or any(needle in c for c in cols)}
    if args.json:
        print(json.dumps({t: {c: dtype for c, dtype in cols.values()} for t, cols in tables.items()}, indent=2))
        return 0
    for name, cols in tables.items():
        if args.columns or len(tables) <= 5:
            print(f"{name}: " + ", ".join(f"{c} ({dtype})" for c, dtype in cols.values()))
        else:
            print(name)
    print(f"{len(tables)} table(s) match in {db} (structure only, no row data)")
    return 0


def cmd_check_playbooks(args: argparse.Namespace, settings: Settings, connect: Connect | None = None,
                        **_: Any) -> int:
    """Validate playbooks and, with --db, test every SQL check's table names on a real account book."""
    try:
        playbooks, problems = load_playbooks(settings.playbook_dir)
    except PlaybookError as e:
        print(str(e), file=sys.stderr)
        return 2
    result: dict[str, Any] = {"playbooks": len(playbooks), "problems": problems, "sql_checks": []}
    db = args.db or settings.database
    if db:
        sql = SqlServerConnector(settings, Masker(), db, connect=connect)
        try:
            names = [name for name, _ in sql.catalog().values()]
            for pb in playbooks:
                for check in pb.checks:
                    if check["type"] != "sql":
                        continue
                    missing_all: list[str] = []
                    entry = {"playbook": pb.id, "check": check["id"], "ok": False, "uses": "", "missing": "",
                             "similar": []}
                    for variant in sql_variants(check):
                        mapping, missing = sql.resolve_objects(variant.get("objects"))
                        if not missing:
                            entry["ok"] = True
                            entry["uses"] = ", ".join(f"{k}={v}" for k, v in mapping.items())
                            break
                        missing_all += missing
                    if not entry["ok"]:
                        entry["missing"] = " / ".join(missing_all)
                        wanted = {m.split()[-1].split(".")[0].split("|")[0] for m in missing_all}
                        similar: list[str] = []
                        for w in sorted(wanted):
                            similar += difflib.get_close_matches(w, names, n=3, cutoff=0.5)
                            similar += [n for n in names if w.lower() in n.lower()][:3]
                        entry["similar"] = list(dict.fromkeys(similar))[:6]
                    result["sql_checks"].append(entry)
        except SqlUnavailable as e:
            print(f"Cannot check against {db}: {e}", file=sys.stderr)
            return 2
        finally:
            sql.close()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{len(playbooks)} playbook(s) loaded and valid.")
        for p in problems:
            print(f"  ! local playbook {p}")
        if not db:
            print("Add --db AED_NAME to test each SQL check's table names on a real account book.")
        for row in result["sql_checks"]:
            if row["ok"]:
                print(f"[OK]      {row['playbook']}.{row['check']}: {row['uses']}")
            else:
                print(f"[MISSING] {row['playbook']}.{row['check']}: {row['missing']}")
                if row["similar"]:
                    print(f"            similar tables here: {', '.join(row['similar'])}")
        bad = sum(not row["ok"] for row in result["sql_checks"])
        if db:
            print(f"{len(result['sql_checks']) - bad} of {len(result['sql_checks'])} SQL check(s) work on {db}."
                  + (" Send this output back so the missing names can be corrected." if bad else ""))
    return 1 if problems else 0


def cmd_setup_login(args: argparse.Namespace, settings: Settings, connect: Connect | None = None,
                    check_location: bool = True) -> int:
    """Write the script that creates the read-only login. It is never run by this tool."""
    script_path = settings.setup_dir / setup_login.SCRIPT_NAME
    if args.cleanup:
        if script_path.exists():
            script_path.unlink()
            print(f"Deleted {script_path}")
        else:
            print("Nothing to delete.")
        return 0
    books = list(args.book or [])
    if not books:
        try:
            dbs = SqlServerConnector(settings, Masker(), None, connect=connect).list_databases()
            books = [d["name"] for d in dbs if d["looks_like_account_book"]]
        except SqlUnavailable:
            books = []
    if not books:
        print("No account book found. Name them: autocount-assist setup-login --book AED_NAME", file=sys.stderr)
        return 2
    try:
        script = setup_login.build_script(args.login, password := setup_login.generate_password(), books)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"This prepares the read-only login '{args.login}' for: {', '.join(books)}")
    print("Nothing is changed in SQL Server. You run the script yourself.")
    if not confirm(f"Write the script (it contains a new random password) to {script_path}, and save the login "
                   "in your Windows user environment variables?", args.yes):
        return 1
    if check_location:
        ensure_safe_data_dir(settings.setup_dir)
    setup_login.write_script(settings.setup_dir, script)
    stored = setup_login.store_credentials(args.login, password)
    print(f"1. Script written: {script_path}")
    print("2. Open it in SQL Server Management Studio (connected as an administrator) and press F5.")
    if stored:
        print("3. AC_SQL_USER and AC_SQL_PASSWORD are saved for your Windows user. Open a NEW terminal.")
    else:
        print("3. Set AC_SQL_USER and AC_SQL_PASSWORD yourself; the password is inside the script.")
    print("4. Delete the script: autocount-assist setup-login --cleanup")
    print(f"5. Check: autocount-assist doctor --db {books[0]}")
    return 0


def _writable_store(settings: Settings, check_location: bool) -> KnowledgeStore:
    return KnowledgeStore(settings.kb_path, Masker(), allow_write=True, check_location=check_location)


def cmd_learn(args: argparse.Namespace, settings: Settings, connect: Connect | None = None,
              check_location: bool = True) -> int:
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
        if not confirm("Download AutoCount 2.2 release notes from wiki.autocountsoft.com and save them to "
                       f"{settings.kb_path}?", args.yes):
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


def cmd_cases(args: argparse.Namespace, settings: Settings, **_: Any) -> int:
    store = KnowledgeStore(settings.kb_path, Masker())
    rows = store.cases(args.playbook)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No confirmed cases yet.")
    for r in rows:
        print(f"#{r['id']} {r['created_at']} [{r['playbook_id']}/{r['cause_id']}]\n  Scenario: {r['scenario']}\n"
              f"  Cause: {r['cause']}\n  Fix: {r['fix']}")
    return 0


def cmd_demo(args: argparse.Namespace, settings: Settings, **_: Any) -> int:
    from .demo import build_env, scenario_ids

    ids = scenario_ids() if args.scenario == "all" else [args.scenario]
    playbooks, _problems = load_playbooks()
    for sid in ids:
        if sid not in scenario_ids():
            print(f"Unknown scenario '{sid}'. Choose from: {', '.join(scenario_ids())}", file=sys.stderr)
            return 2
        with tempfile.TemporaryDirectory() as tmp:
            env = build_env(sid, Path(tmp))
            conn = env.connection
            ctx = make_context(env.settings, "AED_DEMO", connect=lambda _s, c=conn: c,  # type: ignore[misc]
                               check_location=False, service_state=env.service_state)
            report = build_report(analyse(env.spec["question"], ctx, playbooks), ctx)
            print(report.to_json() if args.json else f"=== Demo: {sid} (synthetic data) ===\n" + render_text(report))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autocount-assist", description="Read-only AutoCount desktop support assistant")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", help="Path to autocount-assist.toml")
    sub = p.add_subparsers(dest="command", required=True)

    def db_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--db", help="Account book database name (e.g. AED_ACME)")
        sp.add_argument("--no-db", action="store_true", help="Skip database checks")

    def since_opt(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--since", help="Only use log lines this recent: 7d, 12h, 2026-11-01 or all "
                                        "(default: the last log_days days, 30)")

    for name, text in (("ask", "Analyse a question or customer scenario (read-only)"),
                       ("propose", "Save proposed data-fix scripts for review (asks first; runs nothing)")):
        a = sub.add_parser(name, help=text)
        a.add_argument("question")
        db_opts(a)
        since_opt(a)
        a.add_argument("--playbook", help="Force a specific playbook id")
        if name == "ask":
            a.add_argument("--json", action="store_true")
            a.add_argument("--ticket-note", action="store_true", help="Also print a short note for the ticket")
        else:
            a.add_argument("--yes", action="store_true", help="Agree to save without prompting")

    s = sub.add_parser("scan", help="Summarise the AutoCount environment")
    db_opts(s)
    since_opt(s)
    s.add_argument("--json", action="store_true")
    s.add_argument("--binaries", action="store_true", help="List every DLL/EXE with its version")

    sub.add_parser("dbs", help="List databases on the SQL Server instance")

    d = sub.add_parser("doctor", help="Check the setup and say what to fix")
    d.add_argument("--db", help="Account book to test the login against")
    d.add_argument("--json", action="store_true")

    q = sub.add_parser("query", help="Run one read-only SELECT (guarded, masked)")
    q.add_argument("sql")
    db_opts(q)
    q.add_argument("--limit", type=int, default=50)
    q.add_argument("--json", action="store_true")

    lg = sub.add_parser("logs", help="Search AutoCount logs (masked)")
    lg.add_argument("pattern", nargs="?", help="Text or regular expression (default: errors)")
    since_opt(lg)
    lg.add_argument("--limit", type=int, default=20)
    lg.add_argument("--context", type=int, default=0, help="Lines to show after each match (stack trace)")
    lg.add_argument("--json", action="store_true")

    cf = sub.add_parser("config", help="Show AutoCount config settings (secrets masked)")
    cf.add_argument("key", nargs="?", help="Text or regular expression to match the setting name")
    cf.add_argument("--limit", type=int, default=200)
    cf.add_argument("--json", action="store_true")

    sc = sub.add_parser("schema", help="Find tables and columns in an account book (structure only)")
    sc.add_argument("pattern", nargs="?", help="Part of a table or column name")
    db_opts(sc)
    sc.add_argument("--columns", action="store_true", help="Always list the columns")
    sc.add_argument("--json", action="store_true")

    cp = sub.add_parser("check-playbooks", help="Validate playbooks; with --db, test their table names")
    cp.add_argument("--db")
    cp.add_argument("--json", action="store_true")

    sl = sub.add_parser("setup-login", help="Prepare the read-only SQL login (writes a script; asks first)")
    sl.add_argument("--book", action="append", help="Account book database (repeat for several)")
    sl.add_argument("--login", default=setup_login.DEFAULT_LOGIN)
    sl.add_argument("--yes", action="store_true")
    sl.add_argument("--cleanup", action="store_true", help="Delete the script after you have run it")

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

    dm = sub.add_parser("demo", help="Run a sample scenario on synthetic data")
    dm.add_argument("scenario", help="Scenario id or 'all'")
    dm.add_argument("--json", action="store_true")
    return p


COMMANDS: dict[str, Callable[..., int]] = {
    "ask": cmd_ask, "propose": cmd_propose, "scan": cmd_scan, "dbs": cmd_dbs, "doctor": cmd_doctor,
    "query": cmd_query, "logs": cmd_logs, "config": cmd_config, "schema": cmd_schema,
    "check-playbooks": cmd_check_playbooks, "setup-login": cmd_setup_login, "learn": cmd_learn,
    "cases": cmd_cases, "demo": cmd_demo,
}


def main(argv: list[str] | None = None, connect: Connect | None = None, check_location: bool = True) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `autocount-assist "<question>"` is short for `autocount-assist ask "<question>"`.
    if argv and not argv[0].startswith("-") and argv[0] not in COMMANDS:
        argv.insert(0, "ask")
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        return COMMANDS[args.command](args, settings, connect=connect, check_location=check_location)
    except (ConfigError, PlaybookError) as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def entry() -> None:
    """Console entry point. Never crash on a character the Windows console cannot show."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass
    sys.exit(main())


if __name__ == "__main__":
    entry()
