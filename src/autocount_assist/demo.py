"""Build a synthetic AutoCount environment from a sample scenario (used by `demo` and tests)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .config import Settings
from .fakedb import FakeConnection


def write_fake_binary(path: Path, version: str) -> Path:
    """Fake PE file carrying UTF-16 VS_VERSIONINFO strings, readable by plugins.read_versions()."""
    def entry(key: str) -> bytes:
        return key.encode("utf-16-le") + b"\x00\x00" + version.encode("utf-16-le") + b"\x00\x00"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + b"\x90" * 64 + entry("FileVersion") + entry("ProductVersion") + b"\x00" * 16)
    return path


def scenario_ids() -> list[str]:
    pkg = resources.files("autocount_assist").joinpath("samples")
    return sorted(p.name[:-5] for p in pkg.iterdir() if p.name.endswith(".yaml"))


def load_scenario(scenario_id: str) -> dict[str, Any]:
    text = resources.files("autocount_assist").joinpath("samples", f"{scenario_id}.yaml").read_text(encoding="utf-8")
    return dict(yaml.safe_load(text))


@dataclass
class DemoEnv:
    spec: dict[str, Any]
    settings: Settings
    connection: FakeConnection
    service_state: Callable[[str], str]


def build_env(scenario_id: str, root: Path, now: dt.datetime | None = None) -> DemoEnv:
    """Log lines use {d0} (today) and {d1} (yesterday), so samples never fall out of the log window."""
    spec = load_scenario(scenario_id)
    now = now or dt.datetime.now()
    days = {"{d0}": f"{now:%Y-%m-%d}", "{d1}": f"{now - dt.timedelta(days=1):%Y-%m-%d}"}
    install = root / "install"
    for name, version in (spec.get("binaries") or {}).items():
        write_fake_binary(install / name, version)
    for rel, text in {**(spec.get("logs") or {}), **(spec.get("configs") or {})}.items():
        for key, value in days.items():
            text = text.replace(key, value)
        p = install / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    db = spec.get("database") or {}
    conn = FakeConnection(schema=db.get("schema"), files=db.get("files"), row_counts=db.get("row_counts"),
                          server=db.get("server"), login=db.get("login"),
                          responses=[(r["match"], r["rows"]) for r in db.get("responses", [])])
    settings = Settings(install_path=str(install), log_dirs=["{install_path}"], data_dir=root / "data")
    services = dict(spec.get("services") or {})
    return DemoEnv(spec, settings, conn, lambda name: services.get(name, "unknown"))
