"""Guard Bradley's rule: sensitive data never goes into the repository."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".bak", ".mdf", ".ldf", ".env"}


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
        files = out.stdout.split()
        if files:
            return files
    except (OSError, subprocess.CalledProcessError):
        pass
    return [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]


def test_no_data_files_tracked():
    bad = [f for f in tracked_files()
           if Path(f).suffix.lower() in FORBIDDEN_SUFFIXES or Path(f).name in ("autocount-assist.toml", ".env")
           or f.startswith(("proposed/", "kb/", "data/", "scans/", "cases/"))]
    assert bad == []


def test_gitignore_covers_sensitive_data():
    ignore = (ROOT / ".gitignore").read_text()
    for pattern in ("*.sqlite", "proposed/", ".env", "autocount-assist.toml", "*.log"):
        assert pattern in ignore


def test_samples_are_marked_synthetic():
    for f in (ROOT / "src/autocount_assist/samples").glob("*.yaml"):
        assert "Synthetic sample" in f.read_text().splitlines()[0], f
