import sqlite3

import pytest

from autocount_assist.config import ConfigError
from autocount_assist.knowledge.release_notes import fetch_release_notes, wikitext_to_text
from autocount_assist.knowledge.store import KnowledgeStore, ReadOnlyStore
from autocount_assist.masking import Masker


def store(tmp_path, write=True):
    return KnowledgeStore(tmp_path / "kb" / "kb.sqlite", Masker(b"s"), allow_write=write, check_location=False)


def test_read_only_by_default(tmp_path):
    s = store(tmp_path, write=False)
    with pytest.raises(ReadOnlyStore):
        s.add_case("x", "y", "z")
    assert not (tmp_path / "kb").exists()  # nothing written without permission


def test_refuses_data_dir_inside_git_checkout(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(ConfigError):
        KnowledgeStore(tmp_path / "repo" / "kb.sqlite", Masker(), allow_write=True)


def test_cases_are_masked_and_searchable(tmp_path):
    s = store(tmp_path)
    cid = s.add_case("Buyer C20880050010 e-invoice rejected, contact ah.kow@example.com",
                     "Buyer TIN typo", "Corrected TIN on debtor", "einvoice_submission_fails", "buyer_tin_invalid",
                     ["Debtor.TIN: C20880050010"])
    raw = sqlite3.connect(tmp_path / "kb" / "kb.sqlite").execute("SELECT scenario, evidence FROM cases").fetchone()
    assert "C20880050010" not in raw[0] + raw[1] and "ah.kow" not in raw[0]
    assert s.confirmed_count("einvoice_submission_fails", "buyer_tin_invalid") == 1
    hits = s.search("einvoice rejected", kinds=("case",))
    assert hits and hits[0]["ref"] == str(cid)


def test_seed_error_codes_and_schema(tmp_path):
    s = store(tmp_path)
    assert s.search("Login failed")[0]["ref"] == "SQL 18456"
    tables, fks = s.add_schema("AED_X", {"columns": [
        {"TABLE_NAME": "StockDTL", "COLUMN_NAME": "ItemCode", "DATA_TYPE": "nvarchar"},
        {"TABLE_NAME": "StockDTL", "COLUMN_NAME": "Qty", "DATA_TYPE": "decimal"}], "foreign_keys": []})
    assert (tables, fks) == (1, 0) and "stockdtl" in s.table_names()


def test_release_notes_parsing():
    pages = {
        "Category": '<a href="/wiki/Release_Note_2.2.31.44">x</a><a href="/wiki/Release_Note_2.2.32.45">y</a>',
        "2.2.32.45": "== New ==\n* '''e-Invoice''': fixed [[Debtor|debtor]] TIN check\n",
        "2.2.31.44": "== Fix ==\n* Stock costing",
    }

    def fake_fetch(url):
        for k, v in pages.items():
            if k in url.replace("_", "."):
                return v
        return pages["Category"]

    notes = list(fetch_release_notes(fake_fetch))
    assert [n["version"] for n in notes] == ["2.2.32.45", "2.2.31.44"]
    assert "fixed debtor TIN check" in notes[0]["body"]
    assert wikitext_to_text("[https://x.y label]") == "label"
