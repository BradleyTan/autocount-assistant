"""Mask customer data before it is printed, logged or stored.

Three layers:
- mask_text(): pattern-based masking for free text (log lines, config values, notes).
- mask_row(): masking for database rows. Rows are masked strictly: a text value is
  shown only when it looks like a code (no spaces, plain ASCII) and its column is not
  a sensitive one. Free text, such as names, addresses and remarks, becomes a token.
- mask_path(): hides the Windows user name in file paths.

Masked values become stable tokens (same input -> same token within one Masker),
so evidence stays comparable ("the same debtor appears in both rows") without
revealing the value. Pattern masking of free text is best-effort: it cannot know
that an arbitrary word is a person's name, which is why database rows are masked
by column and not by pattern.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import hashlib
import hmac
import re
import secrets
from collections.abc import Iterable
from typing import Any

# ---- free-text patterns ---------------------------------------------------

# key=value secrets in connection strings / configs: keep the key, mask the value
_SECRET = re.compile(
    r"(?i)\b(password|pwd|passwd|secret|client_?secret|api_?key|token|access_?token|refresh_?token)"
    r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^;\s,\"'<>]+)")
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+)?")
# Long base64/hex runs (keys, signatures, certificates). A run that follows a path
# separator is a path segment, not a secret, and is left alone.
_BLOB = re.compile(r"(?<![A-Za-z0-9+/_.\\-])[A-Za-z0-9][A-Za-z0-9+/_-]{47,}={0,2}(?![A-Za-z0-9+/=_-])")
_USER_PATH = re.compile(r"(?i)\b([A-Z]:[\\/]+(?:Users|Documents and Settings)[\\/]+)([^\\/:*?\"<>|\r\n]+)")
_PUBLIC_PROFILES = {"public", "default", "all users", "default user"}

# Company names: capitalised words followed by a company suffix.
_COMPANY = re.compile(
    r"((?:(?:[A-Z0-9][\w'.\-]*|&|and)\s+){1,6})"
    r"((?i:Sdn\.?\s*Bhd\.?|Berhad|Bhd\.?|Enterprises?|Trading|PLT|LLP|Holdings|Pte\.?\s*Ltd\.?|Co\.?\s*Ltd\.?|Ltd\.?))"
    r"(?![A-Za-z])")
# Words that label a company in a sentence but are not part of its name.
_COMPANY_LEAD_STOP = {
    "error", "warn", "warning", "info", "debug", "fatal", "exception", "trace", "the", "for", "to", "from",
    "by", "of", "at", "in", "on", "customer", "debtor", "creditor", "supplier", "vendor", "company", "buyer",
    "seller", "name", "account", "invoice", "posting", "posted", "document", "and", "with", "is", "not",
    "found", "failed", "submit", "submitting", "submitted", "validate", "validating", "save", "saving",
}

# Order matters: more specific patterns first.
_ID_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
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
]
# Long digit runs: bank accounts, new 12-digit BRN, card numbers
_LONG_DIGITS = re.compile(r"(?<![\d.])\d{10,19}(?![\d.])")

# ---- database columns -----------------------------------------------------

# A column is sensitive when these words appear in its name, in this order.
# Names are split into words first (CompanyName -> company, name), so "tin" does
# not match PostingDate and "ic" does not match Price.
SENSITIVE_COLUMN_WORDS: tuple[tuple[str, ...], ...] = (
    ("name",), ("attention",), ("attn",), ("address",), ("addr",), ("phone",), ("mobile",), ("tel",),
    ("fax",), ("email",), ("contact",), ("tin",), ("brn",), ("nric",), ("ic",), ("passport",),
    ("identity",), ("password",), ("pwd",), ("remark",), ("remarks",), ("note",), ("notes",), ("memo",),
    ("user",), ("agent",), ("person",), ("postcode",), ("website",), ("signature",),
    ("tax", "register"), ("gst", "register"), ("sst", "register"), ("register", "no"), ("reg", "no"),
    ("bank", "acc"), ("bank", "account"), ("account", "no"), ("card", "no"), ("tax", "payer"),
    ("deliver", "addr"), ("created", "by"), ("modified", "by"),
)
# Columns that would match by word but are structural and safe to show.
SAFE_COLUMNS = {
    "table_name", "column_name", "constraint_name", "table_schema", "schema_name", "index_name",
    "object_name", "type_name", "data_type", "database_name", "dbname", "login_name",
    "itemcode", "taxcode", "taxtype", "docno", "doctype", "accno", "location", "uom", "sourcetype",
    "journaltype", "edition", "recovery_model", "collation", "state_desc", "type_desc", "status",
}

_COLUMN_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_CODE_LIKE = re.compile(r"[A-Za-z0-9][\w./#:+\-]{0,59}", re.ASCII)
_DATE_LIKE = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?(?: ?[AP]M)?")


def column_words(column: str) -> list[str]:
    """CompanyName -> [company, name]; TAX_REGISTER_NO -> [tax, register, no]; TINNo -> [tin, no]."""
    return [w.lower() for part in re.split(r"[^A-Za-z0-9]+", column) for w in _COLUMN_WORD.findall(part)]


# "note" marks free text (Note, InternalNote) but not a document type (DebitNoteNo).
_NOT_FREE_TEXT_BEFORE_NOTE = {"debit", "credit", "delivery", "consignment"}


def _contains(words: list[str], seq: tuple[str, ...]) -> bool:
    n = len(seq)
    for i in range(len(words) - n + 1):
        if tuple(words[i:i + n]) != seq:
            continue
        if seq in (("note",), ("notes",)) and i > 0 and words[i - 1] in _NOT_FREE_TEXT_BEFORE_NOTE:
            continue
        return True
    return False


def _looks_like_timestamp(digits: str) -> bool:
    """20261006085812 and 202610060858 are timestamps, not account numbers."""
    for fmt, length in (("%Y%m%d%H%M%S", 14), ("%Y%m%d%H%M", 12)):
        if len(digits) == length:
            try:
                year = _dt.datetime.strptime(digits, fmt).year
            except ValueError:
                continue
            return 1990 <= year <= 2100
    return False


class Masker:
    def __init__(self, salt: bytes | None = None):
        self._salt = salt or secrets.token_bytes(16)

    def token(self, kind: str, value: str) -> str:
        digest = hmac.new(self._salt, value.strip().lower().encode(), hashlib.sha256).hexdigest()
        return f"<{kind}#{digest[:4]}>"

    # ---- free text ---------------------------------------------------------

    def mask_text(self, text: str | None) -> str | None:
        if not text:
            return text
        out = _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}<SECRET>", text)
        out = _BEARER.sub(lambda m: f"{m.group(1)} <SECRET>", out)
        out = _JWT.sub("<TOKEN>", out)
        out = self.mask_path(out)
        out = _COMPANY.sub(self._mask_company, out)
        for kind, pattern in _ID_PATTERNS:
            out = pattern.sub(lambda m, k=kind: self.token(k, m.group(0)), out)  # type: ignore[misc]
        out = _LONG_DIGITS.sub(
            lambda m: m.group(0) if _looks_like_timestamp(m.group(0)) else self.token("ACCT", m.group(0)), out)
        return _BLOB.sub("<BLOB>", out)

    def _mask_company(self, m: re.Match[str]) -> str:
        lead, suffix = m.group(1), m.group(2)
        words = lead.split()
        keep = 0
        while keep < len(words) and (words[keep].strip(".:,").lower() in _COMPANY_LEAD_STOP):
            keep += 1
        if keep == len(words):  # nothing but label words before the suffix: not a name
            return m.group(0)
        name = " ".join(words[keep:] + [suffix])
        kept = " ".join(words[:keep])
        return (kept + " " if kept else "") + self.token("COMPANY", name)

    def mask_path(self, text: str) -> str:
        """C:\\Users\\Mei Ling\\AppData\\... -> C:\\Users\\<USER>\\AppData\\..."""
        def repl(m: re.Match[str]) -> str:
            return m.group(0) if m.group(2).lower() in _PUBLIC_PROFILES else m.group(1) + "<USER>"
        return _USER_PATH.sub(repl, text)

    # ---- database rows -----------------------------------------------------

    @staticmethod
    def is_sensitive_column(column: str) -> bool:
        if column.lower() in SAFE_COLUMNS:
            return False
        words = column_words(column)
        return any(_contains(words, seq) for seq in SENSITIVE_COLUMN_WORDS)

    def mask_value(self, column: str, value: Any, unmasked: Iterable[str] = ()) -> Any:
        """Mask one database value.

        unmasked: column names the playbook author marked as safe free text (for
        example a SQL Server edition name). Pattern masking still applies to them.
        """
        if value is None or isinstance(value, bool):
            return value
        if self.is_sensitive_column(column):
            kind = re.sub(r"[^A-Z0-9]", "", column.upper())[:16] or "VALUE"
            return self.token(kind, str(value))
        if isinstance(value, (int, float, decimal.Decimal, _dt.date, _dt.time, _dt.timedelta)):
            return value
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "<BINARY>"
        text = str(value)
        lowered = column.lower()
        if lowered in SAFE_COLUMNS or lowered in {u.lower() for u in unmasked}:
            return self.mask_text(text)
        stripped = text.strip()
        if not stripped or _DATE_LIKE.fullmatch(stripped) or _CODE_LIKE.fullmatch(stripped):
            return self.mask_text(text)
        return self.token("TEXT", text)

    def mask_row(self, row: dict[str, Any], unmasked: Iterable[str] = ()) -> dict[str, Any]:
        return {k: self.mask_value(k, v, unmasked) for k, v in row.items()}

    def mask_obj(self, obj: Any) -> Any:
        """Pattern-mask every string inside nested lists/dicts (for scan output)."""
        if isinstance(obj, str):
            return self.mask_text(obj)
        if isinstance(obj, dict):
            return {k: self.mask_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.mask_obj(v) for v in obj]
        return obj
