"""Setup self-check: is everything in place for the assistant to work? Reads only."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from .config import ENV_PASSWORD, ENV_USER, Settings, inside_git_checkout
from .connectors.logs import LogConnector
from .connectors.plugins import PluginConnector
from .connectors.sqlserver import SqlServerConnector, SqlUnavailable, available_drivers, pick_driver
from .masking import Masker
from .setup_login import SCRIPT_NAME

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Finding:
    status: str
    check: str
    detail: str
    fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_doctor(settings: Settings, database: str | None = None,
               connect: Callable[[str], Any] | None = None,
               drivers: list[str] | None = None) -> list[Finding]:
    out: list[Finding] = []
    masker = Masker()
    v = sys.version_info
    out.append(Finding(PASS if v >= (3, 11) else FAIL, "Python", f"{v.major}.{v.minor}.{v.micro}",
                       "" if v >= (3, 11) else "Install Python 3.11 or newer from python.org."))

    install = settings.expand(settings.install_path)
    plugins = PluginConnector(settings.expanded(settings.plugin_dirs))
    if not install.is_dir():
        out.append(Finding(FAIL, "AutoCount folder", f"{install} not found",
                           "Set install_path in the config file to the AutoCount program folder."))
    else:
        exe = plugins.find(settings.app_exes)
        if exe and (exe.product_version or exe.file_version):
            out.append(Finding(PASS, "AutoCount program", f"{exe.name} version {exe.product_version or exe.file_version}"))
        else:
            out.append(Finding(WARN, "AutoCount program", f"none of {', '.join(settings.app_exes)} found in {install}",
                               "Set app_exes (the main EXE name) or app_version in the config file."))

    logs = LogConnector(settings.expanded(settings.log_dirs), masker).discover()
    out.append(Finding(PASS if logs else WARN, "Log files", f"{len(logs)} found",
                       "" if logs else "Add AutoCount's log folder to log_dirs in the config file."))

    data_dir = settings.data_dir
    if inside_git_checkout(data_dir):
        out.append(Finding(FAIL, "Data folder", f"{masker.mask_path(str(data_dir))} is inside a git checkout",
                           "Set data_dir to a local folder outside any repository."))
    else:
        out.append(Finding(PASS, "Data folder", f"{masker.mask_path(str(data_dir))} (local, outside git)"))
    leftover = settings.setup_dir / SCRIPT_NAME
    if leftover.exists():
        out.append(Finding(WARN, "Login script", f"{masker.mask_path(str(leftover))} still contains the password",
                           "After running it in SSMS, delete it: autocount-assist setup-login --cleanup"))

    have_connect = connect is not None
    if not have_connect:
        names = drivers if drivers is not None else available_drivers()
        driver = pick_driver(settings.odbc_driver, names)
        if not names:
            out.append(Finding(FAIL, "ODBC driver", "pyodbc or the SQL Server ODBC driver is not installed",
                               "Install 'ODBC Driver 18 for SQL Server' from Microsoft, then: py -m pip install pyodbc"))
        elif driver is None or (settings.odbc_driver.lower() != "auto" and driver not in names):
            out.append(Finding(FAIL, "ODBC driver", f"'{settings.odbc_driver}' is not installed",
                               "Install 'ODBC Driver 18 for SQL Server', or set odbc_driver = \"auto\"."))
        else:
            out.append(Finding(PASS, "ODBC driver", driver))

    user = settings.sql_user()
    if user and not settings.sql_password():
        out.append(Finding(FAIL, "Credentials", f"{ENV_USER} is set but {ENV_PASSWORD} is empty",
                           "Run: autocount-assist setup-login (then open a new terminal)."))
    elif user:
        out.append(Finding(PASS, "Credentials", f"SQL login '{user}' from environment variables"))
    else:
        out.append(Finding(WARN, "Credentials", "no SQL login set; Windows authentication will be used",
                           "For a dedicated read-only login run: autocount-assist setup-login"))

    server = SqlServerConnector(settings, masker, None, connect=connect)
    try:
        dbs = server.list_databases()
    except SqlUnavailable as e:
        out.append(Finding(FAIL, "SQL Server", f"{settings.sql_server}: {masker.mask_text(str(e))}",
                           "Check the SQL Server service is running and sql_server is right. "
                           "For error 18456 run: autocount-assist setup-login"))
        return out
    finally:
        server.close()
    books = [d["name"] for d in dbs if d["looks_like_account_book"]]
    out.append(Finding(PASS if books else WARN, "SQL Server",
                       f"{settings.sql_server} reachable, {len(books)} account book(s)",
                       "" if books else "No AED_ database is visible to this login."))

    database = database or settings.database
    if not database:
        out.append(Finding(WARN, "Account book", "none selected, so the login's rights were not checked",
                           "Run: autocount-assist doctor --db AED_NAME"))
        return out
    book = SqlServerConnector(settings, masker, database, connect=connect)
    try:
        info = book.login_info()
        if info["can_write"]:
            status = WARN if settings.allow_privileged_login else FAIL
            out.append(Finding(status, "Read-only login",
                               f"'{info.get('login_name')}' can change data in {database} ({', '.join(info['write_rights'])})",
                               "Create a read-only login: autocount-assist setup-login --book " + database))
        else:
            out.append(Finding(PASS, "Read-only login", f"'{info.get('login_name')}' cannot change data in {database}"))
            tables = len(book.catalog())
            out.append(Finding(PASS if tables else FAIL, "Account book", f"{database}: {tables} tables readable",
                               "" if tables else "The login has no read access. Run setup-login for this book."))
    except SqlUnavailable as e:
        out.append(Finding(FAIL, "Account book", f"{database}: {masker.mask_text(str(e))}",
                           "Check the name with `autocount-assist dbs`, and that the login is mapped to it."))
    finally:
        book.close()
    return out


def render(findings: list[Finding]) -> str:
    lines = []
    for f in findings:
        lines.append(f"[{f.status}] {f.check}: {f.detail}")
        if f.fix and f.status != PASS:
            lines.append(f"       -> {f.fix}")
    fails = sum(f.status == FAIL for f in findings)
    warns = sum(f.status == WARN for f in findings)
    lines.append("")
    lines.append("Ready to use." if not fails and not warns else
                 f"{fails} problem(s) to fix, {warns} warning(s)." if fails else f"Usable, with {warns} warning(s).")
    return "\n".join(lines) + "\n"
