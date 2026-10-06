"""Local SQLite knowledge base (with FTS5 search).

Opened read-only unless the caller passes allow_write=True, which the CLI only does
after the user agrees to save. Everything written is masked first.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from ..config import ensure_safe_data_dir
from ..masking import Masker

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS schema_columns (
    db TEXT, table_name TEXT, column_name TEXT, data_type TEXT, max_len INTEGER, nullable TEXT,
    PRIMARY KEY (db, table_name, column_name));
CREATE TABLE IF NOT EXISTS relationships (
    db TEXT, constraint_name TEXT, parent_table TEXT, parent_column TEXT, ref_table TEXT, ref_column TEXT);
CREATE TABLE IF NOT EXISTS error_codes (
    code TEXT PRIMARY KEY, source TEXT, message TEXT, cause TEXT, fix TEXT);
CREATE TABLE IF NOT EXISTS release_notes (
    version TEXT PRIMARY KEY, title TEXT, url TEXT, body TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, playbook_id TEXT, cause_id TEXT,
    scenario TEXT, cause TEXT, fix TEXT, evidence TEXT, app_version TEXT, db_version TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(kind, ref, title, body);
"""


STOPWORDS = set("""the and for not but are was were has have had with from this that these those
into onto than then them they their there what when where which while who why how can cannot could
should would will does did doing done all any some our your you his her its our out off over under
after before since says said say customer customers user users system says fails failed error""".split())


class ReadOnlyStore(Exception):
    pass


class KnowledgeStore:
    def __init__(self, path: Path, masker: Masker, allow_write: bool = False, check_location: bool = True):
        self.path = Path(path)
        self.masker = masker
        self.allow_write = allow_write
        if allow_write:
            if check_location:
                ensure_safe_data_dir(self.path.parent)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path)
            self.db.executescript(SCHEMA)
            if not self.db.execute("SELECT 1 FROM error_codes LIMIT 1").fetchone():
                self._seed_error_codes()
            self.db.commit()
        elif self.path.exists():
            self.db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        else:
            # No KB yet: use an empty in-memory one so reads simply find nothing.
            self.db = sqlite3.connect(":memory:")
            self.db.executescript(SCHEMA)
            self._seed_error_codes()
        self.db.row_factory = sqlite3.Row

    def close(self):
        self.db.close()

    def _require_write(self):
        if not self.allow_write:
            raise ReadOnlyStore("Knowledge base is open read-only; saving needs your permission.")

    # ---- writes ----------------------------------------------------------

    def _index(self, kind: str, ref: str, title: str, body: str):
        self.db.execute("DELETE FROM kb_fts WHERE kind = ? AND ref = ?", (kind, ref))
        self.db.execute("INSERT INTO kb_fts (kind, ref, title, body) VALUES (?, ?, ?, ?)", (kind, ref, title, body))

    def _seed_error_codes(self):
        text = resources.files("autocount_assist.knowledge").joinpath("seed_error_codes.yaml").read_text()
        for e in yaml.safe_load(text) or []:
            self._put_error_code(e["code"], e.get("source", ""), e.get("message", ""), e.get("cause", ""), e.get("fix", ""))

    def _put_error_code(self, code, source, message, cause, fix):
        m = self.masker.mask_text
        row = (code, source, m(message), m(cause), m(fix))
        self.db.execute("INSERT OR REPLACE INTO error_codes VALUES (?, ?, ?, ?, ?)", row)
        self._index("error_code", code, f"{code} {row[2]}", f"{row[3]} {row[4]}")

    def add_error_code(self, code: str, source: str, message: str, cause: str, fix: str):
        self._require_write()
        self._put_error_code(code, source, message, cause, fix)
        self.db.commit()

    def add_schema(self, db_name: str, schema: dict[str, Any]) -> tuple[int, int]:
        self._require_write()
        self.db.execute("DELETE FROM schema_columns WHERE db = ?", (db_name,))
        self.db.execute("DELETE FROM relationships WHERE db = ?", (db_name,))
        tables: dict[str, list[str]] = {}
        for c in schema.get("columns", []):
            self.db.execute("INSERT OR REPLACE INTO schema_columns VALUES (?, ?, ?, ?, ?, ?)", (
                db_name, c["TABLE_NAME"], c["COLUMN_NAME"], c.get("DATA_TYPE"),
                c.get("CHARACTER_MAXIMUM_LENGTH"), c.get("IS_NULLABLE")))
            tables.setdefault(c["TABLE_NAME"], []).append(c["COLUMN_NAME"])
        for fk in schema.get("foreign_keys", []):
            self.db.execute("INSERT INTO relationships VALUES (?, ?, ?, ?, ?, ?)", (
                db_name, fk["constraint_name"], fk["parent_table"], fk["parent_column"],
                fk["ref_table"], fk["ref_column"]))
        for t, cols in tables.items():
            self._index("table", t, t, " ".join(cols))
        self.db.commit()
        return len(tables), len(schema.get("foreign_keys", []))

    def add_release_note(self, version: str, title: str, url: str, body: str):
        self._require_write()
        body = self.masker.mask_text(body) or ""
        self.db.execute("INSERT OR REPLACE INTO release_notes VALUES (?, ?, ?, ?, ?)",
                        (version, title, url, body, _now()))
        self._index("release_note", version, title, body)
        self.db.commit()

    def add_case(self, scenario: str, cause: str, fix: str, playbook_id: str = "", cause_id: str = "",
                 evidence: list[str] | None = None, app_version: str = "", db_version: str = "") -> int:
        self._require_write()
        m = self.masker.mask_text
        ev = json.dumps([m(e) for e in (evidence or [])])
        cur = self.db.execute(
            "INSERT INTO cases (created_at, playbook_id, cause_id, scenario, cause, fix, evidence, app_version, db_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), playbook_id, cause_id, m(scenario), m(cause), m(fix), ev, app_version, db_version))
        case_id = cur.lastrowid
        self._index("case", str(case_id), m(scenario), f"{m(cause)} {m(fix)} {playbook_id} {cause_id}")
        self.db.commit()
        return case_id

    # ---- reads -----------------------------------------------------------

    def search(self, query: str, limit: int = 5, kinds: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        words = [w for w in re.findall(r"[A-Za-z0-9]+", query.lower()) if len(w) >= 3 and w not in STOPWORDS]
        if not words:
            return []
        match = " OR ".join(f'"{w}"' for w in dict.fromkeys(words))
        sql = "SELECT kind, ref, title, snippet(kb_fts, 3, '[', ']', ' … ', 12) AS snippet FROM kb_fts WHERE kb_fts MATCH ?"
        params: list[Any] = [match]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += list(kinds)
        sql += " ORDER BY bm25(kb_fts) LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.execute(sql, params)]

    def cases(self, playbook_id: str | None = None) -> list[dict[str, Any]]:
        if playbook_id:
            rows = self.db.execute("SELECT * FROM cases WHERE playbook_id = ? ORDER BY id DESC", (playbook_id,))
        else:
            rows = self.db.execute("SELECT * FROM cases ORDER BY id DESC")
        return [dict(r) for r in rows]

    def case(self, case_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        return dict(row) if row else None

    def confirmed_count(self, playbook_id: str, cause_id: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM cases WHERE playbook_id = ? AND cause_id = ?",
                               (playbook_id, cause_id)).fetchone()[0]

    def table_names(self) -> set[str]:
        return {r[0].lower() for r in self.db.execute("SELECT DISTINCT table_name FROM schema_columns")}

    def stats(self) -> dict[str, int]:
        return {t: self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("schema_columns", "relationships", "error_codes", "release_notes", "cases")}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
