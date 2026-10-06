"""Shared data types."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Evidence:
    source: str  # "log" | "config" | "sql" | "plugin" | "version" | "kb"
    ref: str  # file:line, table.column, config key, ...
    detail: str  # masked excerpt

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CheckResult:
    check_id: str
    status: str  # "found" | "clear" | "missing"
    description: str
    evidence: list[Evidence] = field(default_factory=list)
    note: str = ""  # why it is missing, or what was checked
    verified: bool = True  # False when it relies on AutoCount table names not yet confirmed

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence"] = [e.to_dict() for e in self.evidence]
        return d
