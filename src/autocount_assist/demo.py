"""Build a synthetic AutoCount environment from a sample scenario (used by `demo` and tests)."""

from __future__ import annotations

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
    text = resources.files("autocount_assist").joinpath("samples", f"{scenario_id}.yaml").read_text()
    return yaml.safe_load(text)


@dataclass
class DemoEnv:
    spec: dict[str, Any]
    settings: Settings
    connection: FakeConnection


def build_env(scenario_id: str, root: Path) -> DemoEnv:
    spec = load_scenario(scenario_id)
    install = root / "install"
    for name, version in (spec.get("binaries") or {}).items():
        write_fake_binary(install / name, version)
    for rel, text in {**(spec.get("logs") or {}), **(spec.get("configs") or {})}.items():
        p = install / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    db = spec.get("database") or {}
    conn = FakeConnection(schema=db.get("schema"),
                          responses=[(r["match"], r["rows"]) for r in db.get("responses", [])])
    settings = Settings(install_path=str(install), log_dirs=["{install_path}"], data_dir=root / "data")
    return DemoEnv(spec, settings, conn)
