import datetime
import decimal

import pytest

from autocount_assist.masking import Masker, column_words


def m():
    return Masker(salt=b"fixed-salt-for-tests")


def test_masks_identifiers_in_free_text():
    text = ("Buyer TIN C20880050010 IC 850101-14-5678 SST W10-1808-32000001 "
            "email ah.kow@example.com phone 012-345 6789 bank 5140123456789 BRN 123456-A")
    out = m().mask_text(text)
    for secret in ["C20880050010", "850101-14-5678", "W10-1808-32000001", "ah.kow@example.com",
                   "012-345 6789", "5140123456789", "123456-A"]:
        assert secret not in out, secret
    assert "<TIN#" in out and "<IC#" in out and "<EMAIL#" in out


def test_masks_credentials_but_keeps_key():
    out = m().mask_text("Server=.\\A2006;User Id=sa;Password=Sup3r!Secret;client_secret: abc123")
    assert "Sup3r!Secret" not in out and "abc123" not in out
    assert "Password=<SECRET>" in out


def test_masks_tokens_and_long_keys():
    jwt = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlX2J5dGVz"
    key = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5YWJjZGVmZ2hpamtsbW5vcA=="
    out = m().mask_text(f"Authorization: Bearer {jwt} sent; token body {jwt}; signature={key}")
    assert jwt not in out and key not in out
    assert "Bearer <SECRET>" in out and "<TOKEN>" in out and "<BLOB>" in out


def test_long_paths_and_urls_are_not_treated_as_keys():
    text = ("GET https://api.example.gov.my/api/v1.0/documentsubmissions/pending-validation-requests-list-all-0001 "
            "from /opt/autocount/accounting/logs/einvoice/submission-history-archive-2026/file")
    assert "<BLOB>" not in m().mask_text(text)


def test_version_numbers_dates_and_timestamps_survive():
    out = m().mask_text("AutoCount 2.2.32.45 DB 2.2.97 at 2026-10-06 15:00:01 stamp 20261006150001")
    assert "2.2.32.45" in out and "2.2.97" in out and "2026-10-06" in out and "20261006150001" in out


@pytest.mark.parametrize("line, hidden, kept", [
    ("ERROR Customer Kedai Ah Kow Sdn Bhd not found", "Kedai Ah Kow", "ERROR Customer"),
    ("Posting Invoice for MAJU JAYA TRADING SDN. BHD. failed", "MAJU JAYA", "Posting Invoice for"),
    ("Buyer: Tan & Lee Enterprise", "Tan & Lee", "Buyer:"),
    ("Supplier Global Parts Pte Ltd rejected", "Global Parts", "Supplier"),
])
def test_masks_company_names(line, hidden, kept):
    out = m().mask_text(line)
    assert hidden not in out and "<COMPANY#" in out and kept in out


def test_plain_sentences_are_not_treated_as_companies():
    line = "Error: Trading period is locked for the selected document"
    assert m().mask_text(line) == line


def test_masks_windows_user_name_in_paths():
    out = m().mask_text(r"Log at C:\Users\Mei Ling\AppData\Local\AutoCount\Error.log and C:\Users\Public\x.log")
    assert "Mei Ling" not in out
    assert r"C:\Users\<USER>\AppData\Local\AutoCount\Error.log" in out and r"C:\Users\Public\x.log" in out


def test_tokens_are_stable_within_a_masker_and_differ_between_maskers():
    mk = m()
    assert mk.mask_text("C20880050010") == mk.mask_text("C20880050010")
    assert Masker().mask_text("C20880050010") != Masker().mask_text("C20880050010")


@pytest.mark.parametrize("column, words", [
    ("CompanyName", ["company", "name"]), ("TAX_REGISTER_NO", ["tax", "register", "no"]),
    ("TINNo", ["tin", "no"]), ("PostingDate", ["posting", "date"]), ("Addr1", ["addr", "1"]),
])
def test_column_words(column, words):
    assert column_words(column) == words


@pytest.mark.parametrize("column", [
    "CompanyName", "TIN", "BuyerTIN", "TaxRegisterNo", "Attention", "DeliverAddr1", "Phone1", "EmailAddress",
    "BankAccountNo", "Remark2", "Note", "IC", "NRIC", "UserName", "CreatedUserID", "SalesAgent", "Password",
])
def test_sensitive_columns(column):
    assert Masker.is_sensitive_column(column)


@pytest.mark.parametrize("column", [
    "PostingDate", "IsPosting", "Price", "UnitPrice", "DestinationCode", "LastPrintingTime", "Rating",
    "DebitNoteNo", "ItemCode", "AccNo", "TABLE_NAME", "DocNo", "Qty", "TaxCode", "Location",
])
def test_non_sensitive_columns(column):
    assert not Masker.is_sensitive_column(column)


def test_mask_row_is_strict_about_free_text():
    row = {
        "AccNo": "300-A001", "CompanyName": "Syarikat Maju Sdn Bhd", "TIN": "C123", "Qty": 5,
        "ItemCode": "ITEM-1", "Description": "Deposit from Ah Kow for lorry", "Desc2": "陈大明",
        "Amount": decimal.Decimal("10.50"), "DocDate": datetime.date(2026, 10, 6), "PostDate": "2026-10-06 08:00:00",
        "IsActive": True, "Photo": b"\x00\x01", "Nothing": None, "Phone1": 60123456789,
    }
    out = m().mask_row(row)
    assert out["AccNo"] == "300-A001" and out["Qty"] == 5 and out["ItemCode"] == "ITEM-1"
    assert out["Amount"] == decimal.Decimal("10.50") and out["DocDate"] == datetime.date(2026, 10, 6)
    assert out["PostDate"] == "2026-10-06 08:00:00" and out["IsActive"] is True and out["Nothing"] is None
    assert out["Photo"] == "<BINARY>"
    for col in ("CompanyName", "TIN", "Description", "Desc2", "Phone1"):
        assert str(out[col]).startswith("<") and str(row[col]) not in str(out[col]), col
    assert out["Description"].startswith("<TEXT#")


def test_unmasked_columns_still_get_pattern_masking():
    row = {"Info": "Express Edition (64-bit) owner ah.kow@example.com"}
    assert m().mask_row(row)["Info"].startswith("<TEXT#")
    out = m().mask_row(row, unmasked=["info"])["Info"]
    assert out.startswith("Express Edition (64-bit)") and "ah.kow@example.com" not in out


def test_mask_obj_walks_nested_structures():
    out = m().mask_obj({"files": [r"C:\Users\Ali\x.log"], "n": 3, "deep": {"t": "mail a@b.co"}})
    assert out["files"] == [r"C:\Users\<USER>\x.log"] and out["n"] == 3 and "a@b.co" not in out["deep"]["t"]
