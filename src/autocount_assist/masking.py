"""Mask customer data before it is printed, logged or stored.

Two layers:
- mask_text(): pattern-based masking for free text (log lines, config values, notes).
- mask_row(): column-based masking for database rows, where names and addresses
  can't be recognised by pattern alone.

Masked values become stable tokens (same input -> same token within one Masker),
so evidence stays comparable without revealing the value.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any

# Order matters: more specific patterns first.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # key=value secrets in connection strings / configs: keep the key, mask the value
    ("SECRET", re.compile(
        r"(?i)\b(password|pwd|passwd|secret|client_?secret|api_?key|token|access_?token)"
        r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^;\s,\"'<>]+)")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # Malaysian NRIC: YYMMDD-PB-###G
    ("IC", re.compile(r"\b\d{6}-\d{2}-\d{4}\b")),
    # SST registration no., e.g. W10-1808-32000001
    ("SST", re.compile(r"\b[A-Z]\d{2}-\d{4}-\d{8}\b")),
    # LHDN TIN, e.g. C20880050010, IG12345678901
    ("TIN", re.compile(r"\b(?:IG|C|SG|OG|D|E|F|FA|PT|TA|TC|TN|TR|TP|J|LE)\d{8,13}\b")),
    # Old-style SSM BRN, e.g. 123456-A
    ("BRN", re.compile(r"\b\d{5,7}-[A-Z]\b")),
    ("PHONE", re.compile(r"(?<![\d.])(?:\+?60|0)1\d[- ]?\d{3,4}[- ]?\d{4}\b")),
    # Long digit runs: bank accounts, new 12-digit BRN, card numbers
    ("ACCT", re.compile(r"(?<![\d.])\d{10,19}(?![\d.])")),
]

# Database columns whose values are always masked (case-insensitive substring match).
SENSITIVE_COLUMN_HINTS = (
    "name", "attention", "address", "phone", "mobile", "fax", "email", "contact",
    "tin", "taxregister", "gstregister", "sstregister", "registerno", "brn",
    "identity", "nric", "passport", "bankacc", "bankaccount", "accountno",
    "password", "pwd", "remark", "note",
)
# Columns that look sensitive by hint but are structural and safe to show.
SAFE_COLUMNS = {
    "table_name", "column_name", "constraint_name", "name_of_table", "itemcode",
    "username_masked", "taxcode", "taxtypename", "docno", "doctype", "accno",
    "location", "uom", "sourcetype", "dbname", "database_name",
}


class Masker:
    def __init__(self, salt: bytes | None = None):
        self._salt = salt or secrets.token_bytes(16)

    def token(self, kind: str, value: str) -> str:
        digest = hmac.new(self._salt, value.strip().lower().encode(), hashlib.sha256).hexdigest()
        return f"<{kind}#{digest[:4]}>"

    def mask_text(self, text: str | None) -> str | None:
        if not text:
            return text
        out = text
        for kind, pattern in _PATTERNS:
            if kind == "SECRET":
                out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}<SECRET>", out)
            else:
                out = pattern.sub(lambda m, k=kind: self.token(k, m.group(0)), out)
        return out

    @staticmethod
    def is_sensitive_column(column: str) -> bool:
        c = column.lower()
        if c in SAFE_COLUMNS:
            return False
        return any(h in c for h in SENSITIVE_COLUMN_HINTS)

    def mask_value(self, column: str, value: Any) -> Any:
        if value is None:
            return None
        if self.is_sensitive_column(column):
            kind = re.sub(r"[^A-Z]", "", column.upper())[:12] or "VALUE"
            return self.token(kind, str(value))
        if isinstance(value, str):
            return self.mask_text(value)
        return value

    def mask_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {k: self.mask_value(k, v) for k, v in row.items()}
