"""
Web fallback for questions the user's documents and the official clauses do not
answer. Search is key-less (DuckDuckGo via `ddgs`). Everything fetched from the
web is untrusted text: it is fenced, length-capped, never executed, and never
treated as instructions. Official domains (CITATION_ALLOWLIST) are ranked first
and marked official; everything else is marked not official.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

from .config import CITATION_ALLOWLIST

_UA = "Mozilla/5.0 (compatible; SaralcreditAI-assistant/0.1)"
_OFFICIAL_SUFFIXES = (".gov.in", ".nic.in")
_PAGE_CHARS = 2500


@dataclass
class WebHit:
    url: str
    title: str
    snippet: str
    official: bool
    page_text: str = ""


class _Text(HTMLParser):
    _SKIP = {"script", "style", "nav", "footer", "header", "noscript", "svg", "form"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if not self._depth and data.strip():
            self.parts.append(data.strip())


def _is_official(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in CITATION_ALLOWLIST or host.endswith(_OFFICIAL_SUFFIXES)


def _page_text(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=8)
        if not r.ok or "html" not in r.headers.get("content-type", ""):
            return ""
        p = _Text()
        p.feed(r.text[:400_000])
        return " ".join(p.parts)[:_PAGE_CHARS]
    except requests.RequestException:
        return ""


def search(query: str, max_results: int = 6, fetch_pages: int = 2) -> list[WebHit]:
    """Official sources first. Returns [] on any failure so callers fall back."""
    import time

    from ddgs import DDGS

    raw = []
    for attempt in range(3):  # the free backend rate-limits now and then
        try:
            raw = DDGS().text(query, max_results=max_results, region="in-en")
        except Exception:
            raw = []
        if raw:
            break
        time.sleep(1.5 * (attempt + 1))
    if not raw:
        return []
    hits = [WebHit(url=r.get("href", ""), title=r.get("title", ""), snippet=r.get("body", ""),
                   official=_is_official(r.get("href", ""))) for r in raw if r.get("href")]
    hits.sort(key=lambda h: not h.official)
    for h in hits[:fetch_pages]:
        h.page_text = _page_text(h.url)
    return hits


def fence(hits: list[WebHit]) -> str:
    """Render hits as clearly delimited untrusted data for the model."""
    blocks = []
    for i, h in enumerate(hits, 1):
        body = (h.page_text or h.snippet)[:_PAGE_CHARS]
        blocks.append(f"<web_result n={i} official={'yes' if h.official else 'no'} url={h.url}>\n{body}\n</web_result>")
    return "\n".join(blocks)
