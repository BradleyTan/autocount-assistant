"""Read-only facts about this Windows PC: SQL Server services and where the server runs."""

from __future__ import annotations

import re
import socket
import subprocess
import sys
from collections.abc import Callable

_LOCAL_HOSTS = {"localhost", ".", "(local)", "127.0.0.1", "::1"}
_STATE = re.compile(r"STATE\s*:\s*(\d+)")
_STATES = {"1": "stopped", "2": "starting", "3": "stopping", "4": "running", "7": "paused"}

Runner = Callable[[list[str]], tuple[int, str]]


def split_server(sql_server: str) -> tuple[str, str | None]:
    r"""'localhost\A2006' -> ('localhost', 'A2006'); 'tcp:srv,1433' -> ('srv', None)."""
    host = re.sub(r"^(tcp|np|lpc):", "", sql_server.strip(), flags=re.IGNORECASE)
    host, _, instance = host.partition("\\")
    host = host.split(",")[0].strip()
    return host, (instance.split(",")[0].strip() or None)


def is_local_server(sql_server: str) -> bool:
    host = split_server(sql_server)[0].lower()
    if host in _LOCAL_HOSTS:
        return True
    try:
        return host == socket.gethostname().lower()
    except OSError:
        return False


def sql_service_names(sql_server: str) -> list[str]:
    """Windows service names for the instance: the engine, and the Browser for a named instance."""
    instance = split_server(sql_server)[1]
    if instance and instance.upper() != "MSSQLSERVER":
        return [f"MSSQL${instance}", "SQLBrowser"]
    return ["MSSQLSERVER"]


def _run_sc(args: list[str]) -> tuple[int, str]:
    out = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
    return out.returncode, out.stdout + out.stderr


def service_state(name: str, run: Runner | None = None) -> str:
    """running | stopped | starting | stopping | paused | not_installed | unknown. Only reads."""
    if run is None:
        if sys.platform != "win32":
            return "unknown"
        run = _run_sc
    try:
        code, text = run(["sc.exe", "query", name])
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if code == 1060 or "1060" in text:
        return "not_installed"
    m = _STATE.search(text)
    return _STATES.get(m.group(1), "unknown") if m else "unknown"
