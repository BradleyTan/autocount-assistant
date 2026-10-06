from autocount_assist.masking import Masker


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


def test_version_numbers_and_dates_survive():
    out = m().mask_text("AutoCount 2.2.32.45 DB 2.2.97 at 2026-10-06 15:00:01")
    assert "2.2.32.45" in out and "2.2.97" in out and "2026-10-06" in out


def test_tokens_are_stable_within_a_masker():
    mk = m()
    assert mk.mask_text("C20880050010") == mk.mask_text("C20880050010")


def test_mask_row_masks_name_columns():
    row = {"AccNo": "300-A001", "CompanyName": "Syarikat Maju Sdn Bhd", "TIN": "C123",
           "Qty": 5, "ItemCode": "ITEM-1"}
    out = m().mask_row(row)
    assert out["AccNo"] == "300-A001" and out["Qty"] == 5 and out["ItemCode"] == "ITEM-1"
    assert "Maju" not in str(out["CompanyName"]) and out["TIN"] != "C123"
