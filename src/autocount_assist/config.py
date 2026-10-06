"""Settings: non-secret values from a TOML file, secrets only from environment variables."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_INSTALL_PATH = r"C:\Program Files\AutoCount\Accounting 2.2"
DEFAULT_SQL_SERVER = r"localhost\A2006"
DEFAULT_LOG_DIRS = [
    "{install_path}",
    r"%ProgramData%\AutoCount",
    r"%LocalAppData%\AutoCount",
    r"%AppData%\AutoCount",
]
DEFAULT_APP_EXES = ["AutoCount2.exe", "AutoCount.exe", "Consolidation.exe"]

ENV_SERVER = "AC_SQL_SERVER"
ENV_USER = "AC_SQL_USER"
ENV_PASSWORD = "AC_SQL_PASSWORD"
ENV_DATABASE = "AC_DB"
ENV_CONFIG = "AC_CONFIG"
ENV_DATA_DIR = "AC_DATA_DIR"


class ConfigError(Exception):
    pass


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "autocount-assist"
    return Path.home() / ".local" / "share" / "autocount-assist"


def default_config_path() -> Path:
    return default_data_dir() / "autocount-assist.toml"


@dataclass
class Settings:
    install_path: str = DEFAULT_INSTALL_PATH
    sql_server: str = DEFAULT_SQL_SERVER
    odbc_driver: str = "ODBC Driver 18 for SQL Server"
    database: str | None = None
    log_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_LOG_DIRS))
    config_dirs: list[str] = field(default_factory=lambda: ["{install_path}"])
    plugin_dirs: list[str] = field(default_factory=lambda: ["{install_path}"])
    app_exes: list[str] = field(default_factory=lambda: list(DEFAULT_APP_EXES))
    app_version: str | None = None  # override when the EXE version can't be read
    db_version_query: str | None = None  # override when auto-discovery picks the wrong table
    data_dir: Path = field(default_factory=default_data_dir)

    def expand(self, raw: str) -> Path:
        return Path(os.path.expandvars(raw.replace("{install_path}", self.install_path)))

    def expanded(self, raws: list[str]) -> list[Path]:
        seen: list[Path] = []
        for raw in raws:
            p = self.expand(raw)
            if "%" in str(p):  # unresolved env var on this OS
                continue
            if p not in seen:
                seen.append(p)
        return seen

    @property
    def kb_path(self) -> Path:
        return self.data_dir / "kb.sqlite"

    @property
    def proposed_dir(self) -> Path:
        return self.data_dir / "proposed"

    # Credentials are read on demand and never stored on the object.
    @staticmethod
    def sql_user() -> str | None:
        return os.environ.get(ENV_USER) or None

    @staticmethod
    def sql_password() -> str | None:
        return os.environ.get(ENV_PASSWORD) or None


_SECRET_KEYS = {"password", "pwd", "sql_password", "user", "sql_user", "connection_string"}


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path or os.environ.get(ENV_CONFIG) or default_config_path())
    s = Settings()
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
        leaked = _SECRET_KEYS & {k.lower() for k in data}
        if leaked:
            raise ConfigError(
                f"{path} contains {sorted(leaked)}. Credentials belong in the "
                f"{ENV_USER}/{ENV_PASSWORD} environment variables, not in the config file."
            )
        for key, value in data.items():
            if not hasattr(s, key):
                raise ConfigError(f"Unknown setting '{key}' in {path}")
            if key == "data_dir":
                value = Path(value)
            setattr(s, key, value)
    if os.environ.get(ENV_SERVER):
        s.sql_server = os.environ[ENV_SERVER]
    if os.environ.get(ENV_DATABASE):
        s.database = os.environ[ENV_DATABASE]
    if os.environ.get(ENV_DATA_DIR):
        s.data_dir = Path(os.environ[ENV_DATA_DIR])
    return s


def inside_git_checkout(path: Path) -> bool:
    """True if path (or its nearest existing parent) is inside a git working tree."""
    p = path.resolve()
    while not p.exists():
        p = p.parent
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return True
    try:
        out = subprocess.run(
            ["git", "-C", str(p), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_safe_data_dir(path: Path) -> None:
    """Refuse to keep customer data anywhere it could be committed to git."""
    if inside_git_checkout(path):
        raise ConfigError(
            f"Data folder {path} is inside a git checkout. Sensitive data must stay outside "
            f"any repository; set data_dir (or {ENV_DATA_DIR}) to a local folder such as "
            f"{default_data_dir()}."
        )
