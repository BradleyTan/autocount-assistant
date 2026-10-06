from autocount_assist.connectors.config_files import ConfigConnector
from autocount_assist.connectors.logs import LogConnector
from autocount_assist.connectors.plugins import PluginConnector
from autocount_assist.masking import Masker

from helpers import fake_dll


def test_log_connector_finds_and_masks(tmp_path):
    logdir = tmp_path / "Logs"
    logdir.mkdir()
    (logdir / "ErrorLog.txt").write_text(
        "2026-10-01 09:00:00 INFO started\n"
        "2026-10-01 09:01:00 ERROR Login failed for user 'ac_user'. Buyer IC 850101-14-5678\n"
        "2026-10-01 09:02:00 ERROR second failure\n")
    conn = LogConnector([tmp_path], Masker(b"s"))
    hits = conn.search(r"Login failed")
    assert len(hits) == 1
    assert hits[0].line_no == 2 and hits[0].timestamp == "2026-10-01 09:01:00"
    assert "850101-14-5678" not in hits[0].line
    errors = conn.recent_errors()
    assert errors[0].line.endswith("second failure")  # newest first


def test_log_connector_handles_missing_dirs(tmp_path):
    assert LogConnector([tmp_path / "nope"], Masker()).search("x") == []


def test_log_connector_reads_utf16(tmp_path):
    (tmp_path / "exception.log").write_bytes("2026-10-01 10:00 Exception: boom\n".encode("utf-16"))
    hits = LogConnector([tmp_path], Masker()).search("Exception")
    assert hits and "boom" in hits[0].line


def test_config_connector_parses_and_masks(tmp_path):
    (tmp_path / "AutoCount.exe.config").write_text(
        '<configuration><appSettings>'
        '<add key="EInvoiceEnvironment" value="Sandbox"/>'
        '<add key="EInvoiceClientSecret" value="s3cr3t"/>'
        '</appSettings><connectionStrings>'
        '<add name="Main" connectionString="Server=.\\A2006;User Id=sa;Password=Pa55;"/>'
        '</connectionStrings></configuration>')
    (tmp_path / "settings.ini").write_text("[SQL]\ninstance = localhost\\A2006\n")
    conn = ConfigConnector([tmp_path], Masker())
    env = conn.find(r"EInvoiceEnvironment")
    assert env and env[0].value == "Sandbox"
    secret = conn.find(r"ClientSecret")[0]
    assert secret.value == "<SECRET>"
    cs = conn.find(r"connectionString")[0]
    assert "Pa55" not in cs.value and "A2006" in cs.value
    assert conn.find(r"SQL\.instance")[0].value == "localhost\\A2006"


def test_config_connector_skips_broken_files(tmp_path):
    (tmp_path / "broken.config").write_text("<configuration><unclosed>")
    assert ConfigConnector([tmp_path], Masker()).entries() == []


def test_plugin_connector_reads_versions(tmp_path):
    fake_dll(tmp_path / "AutoCount2.exe", "2.2.32.45")
    fake_dll(tmp_path / "AutoCount.Accounting.dll", "2.2.32.45")
    fake_dll(tmp_path / "Plugins" / "AutoCount.Stock.dll", "2.2.30.40")
    fake_dll(tmp_path / "Plugins" / "VendorPlugin.dll", "1.0.3.0")
    conn = PluginConnector([tmp_path])
    names = {b.name: b.file_version for b in conn.list_binaries()}
    assert names["VendorPlugin.dll"] == "1.0.3.0"
    assert conn.find(["AutoCount2.exe"]).product_version == "2.2.32.45"
    mism = conn.core_version_mismatches("2.2.32.45")
    assert [b.name for b in mism] == ["AutoCount.Stock.dll"]
