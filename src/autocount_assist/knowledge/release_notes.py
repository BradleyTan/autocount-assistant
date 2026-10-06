"""Download AutoCount Accounting 2.2 release notes from the official AutoCount wiki."""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from typing import Callable, Iterator

WIKI = "https://wiki.autocountsoft.com"
CATEGORY = f"{WIKI}/wiki/Category:AutoCount_Accounting_2.2:Release_Note"
_LINK = re.compile(r'href="/wiki/(Release_Note_(2\.2\.\d+\.\d+))"')


def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "autocount-assist/0.1 (support tool)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
        return resp.read().decode("utf-8", errors="replace")


def list_versions(fetch: Callable[[str], str] = http_get) -> list[tuple[str, str]]:
    """[(page_title, version)] newest first."""
    html = fetch(CATEGORY)
    found = dict((v, page) for page, v in _LINK.findall(html))
    return [(found[v], v) for v in sorted(found, key=lambda s: tuple(map(int, s.split("."))), reverse=True)]


def wikitext_to_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)  # [[link|label]]
    text = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", text)  # [url label]
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"^[=*#:;|!{}-]+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_release_notes(fetch: Callable[[str], str] = http_get, only: set[str] | None = None,
                        limit: int | None = None) -> Iterator[dict[str, str]]:
    versions = list_versions(fetch)
    if only:
        versions = [(p, v) for p, v in versions if v in only]
    for page, version in versions[:limit]:
        raw_url = f"{WIKI}/index.php?title={urllib.parse.quote(page)}&action=raw"
        yield {
            "version": version,
            "title": page.replace("_", " "),
            "url": f"{WIKI}/wiki/{page}",
            "body": wikitext_to_text(fetch(raw_url)),
        }
