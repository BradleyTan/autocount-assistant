"""Prepare the read-only SQL login.

This module never talks to SQL Server. It writes a script for you to run yourself in
SQL Server Management Studio, and (on Windows, with your permission) stores the login
name and password in your user environment variables. The password is never printed.
"""

from __future__ import annotations

import re
import secrets
import string
import sys
from pathlib import Path

from .config import ENV_PASSWORD, ENV_USER

DEFAULT_LOGIN = "ac_readonly"
SCRIPT_NAME = "create_readonly_login.sql"
_NAME = re.compile(r"^[A-Za-z0-9_\- .$#@]{1,128}$")
# No quotes, semicolons, braces or shell characters: safe in T-SQL, ODBC strings and consoles.
_SYMBOLS = "!#*+-_.~"


def generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + _SYMBOLS
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in _SYMBOLS for c in pw)):
            return pw


def _check_name(name: str, what: str) -> str:
    if not _NAME.match(name):
        raise ValueError(f"{what} '{name}' has characters that are not allowed")
    return name


def _q(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _n(text: str) -> str:
    return "N'" + text.replace("'", "''") + "'"


def build_script(login: str, password: str, books: list[str]) -> str:
    """T-SQL that creates (or resets) the login and gives it read-only access to each book."""
    login = _check_name(login, "Login name")
    books = [_check_name(b, "Database name") for b in books]
    lines = [
        "/*",
        "  Read-only login for autocount-assist.",
        "  Run this yourself in SQL Server Management Studio as an administrator (press F5).",
        "  autocount-assist never runs it. This file contains the password: delete it afterwards",
        "  (autocount-assist setup-login --cleanup).",
        "*/",
        "USE [master];",
        f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = {_n(login)})",
        f"    CREATE LOGIN {_q(login)} WITH PASSWORD = {_n(password)}, CHECK_POLICY = ON, "
        "CHECK_EXPIRATION = OFF, DEFAULT_DATABASE = [master];",
        "ELSE",
        f"    ALTER LOGIN {_q(login)} WITH PASSWORD = {_n(password)};",
        "GO",
    ]
    for book in books:
        lines += [
            "",
            f"USE {_q(book)};",
            f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = {_n(login)})",
            f"    CREATE USER {_q(login)} FOR LOGIN {_q(login)};",
            f"ALTER ROLE [db_datareader] ADD MEMBER {_q(login)};",
            "-- Deny writes even if someone later adds the user to another role.",
            f"ALTER ROLE [db_denydatawriter] ADD MEMBER {_q(login)};",
            "GO",
        ]
    if not books:
        lines += ["", "-- No account book was named. Run setup-login again with --book AED_NAME."]
    return "\n".join(lines) + "\n"


def write_script(folder: Path, script: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / SCRIPT_NAME
    path.write_text(script, encoding="utf-8")
    return path


def store_credentials(login: str, password: str) -> bool:
    """Save AC_SQL_USER / AC_SQL_PASSWORD for the current Windows user. False when not on Windows."""
    if sys.platform != "win32":
        return False
    import ctypes
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, ENV_USER, 0, winreg.REG_SZ, login)
        winreg.SetValueEx(key, ENV_PASSWORD, 0, winreg.REG_SZ, password)
    try:  # tell running programs that the environment changed
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 2000, None)
    except (OSError, AttributeError):
        pass
    return True
