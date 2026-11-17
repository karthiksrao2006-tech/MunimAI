"""
Fetch official scheme pages and turn them into verbatim clauses.

No model ever touches clause text: pages are fetched, converted to text with the
stdlib HTML parser, split by heading into paragraph-sized pieces, and stored as
JSON exactly as printed. Boilerplate (nav, cookie banners, counters) is dropped
by rule. Each clause carries its source URL, domain and fetch time.

Run:  python -m backend.kb.fetch            # fetch every source in SOURCES
      python -m backend.kb.fetch --inspect URL   # print the clauses for one URL
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests

KB_DIR = Path(__file__).parent
UA = "MunimAI-KB/1.0 (research prototype; official-source clause extraction)"
DELAY_S = 1.5
TIMEOUT_S = 30
MAX_CLAUSE_CHARS = 700
MIN_CLAUSE_CHARS = 60
MAX_CLAUSES_PER_PAGE = 45

# (scheme_code, url). Verified reachable and content-bearing; see SOURCES.md.
_CG = "https://www.cgtmse.in"
_SI = "https://www.startupindia.gov.in"
SOURCES: list[tuple[str, str]] = [
    ("PMEGP", "https://pmegp.msme.gov.in/Home/FAQ"),
    ("PMEGP", "https://pmegp.msme.gov.in/Home/TermsAndConditions"),
    ("PMEGP", "https://pmegp.msme.gov.in/Home/HomePage"),
    ("CGTMSE", f"{_CG}/"),
    ("CGTMSE", f"{_CG}/Home/VS/71"),
    ("CGTMSE", f"{_CG}/Default/ViewFile/?id=1743176302611_CGTMSE+-+Scheme+Document+CGS+I_updated+as+on+Apr+1+2025.pdf&path=Page"),
    ("PM_SVANIDHI", "https://pmsvanidhi.mohua.gov.in"),
    ("STANDUP_INDIA", "https://www.standupmitra.in/"),
    ("PM_VISHWAKARMA", "https://pmvishwakarma.gov.in/"),
    ("NSIC", "https://www.nsic.co.in/Schemes/SinglePointRegistration"),
    ("NSIC", "https://www.nsic.co.in/Schemes/RawMaterialAgainstBG"),
    ("NSIC", "https://www.nsic.co.in/Schemes/MSMESaarthi"),
    ("SIDBI", "https://www.sidbi.in/en/working-capital"),
    ("SIDBI", "https://www.sidbi.in/en/machinery-loan"),
    ("SIDBI", "https://www.sidbi.in/en/business-loan"),
    ("STARTUP_INDIA", f"{_SI}/content/sih/en/startup-scheme.html"),
    ("STARTUP_INDIA", f"{_SI}/content/sih/en/startupgov/startup_recognition_page.html"),
    ("SISFS", f"{_SI}/content/dam/invest-india/Templates/public/Guidelines%20for%20Startup%20India%20Seed%20Fund%20Scheme.pdf"),
    ("STARTUP_PLAYBOOK", f"{_SI}/content/dam/startupindia/homebanners/Startup-Schemes-Playbook-June-2026.pdf"),
]

_SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer",
              "aside", "form", "button", "select", "option", "iframe"}
_BLOCK_TAGS = {"p", "li", "td", "th", "dt", "dd", "div", "section", "article",
               "tr", "br", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BOILER = re.compile(
    r"cookie|copyright|all rights reserved|visitor|last updated|javascript|skip to|"
    r"screen reader|font size|sitemap|privacy policy|terms (&|and) conditions|"
    r"disclaimer|website content|designed (and|&) developed|hyperlink|feedback|"
    r"toll free|follow us|download app|^home$|^login|^register$|^faq$|click here",
    re.I)


_NOISE_HEADING = re.compile(
    r"stor(y|ies)|testimonial|news|gallery|success|event|tender|vacanc|career|"
    r"press|media|video|blog|latest|notification|circular|contact|follow", re.I)


class _Blocks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.cur: list[str] = []
        self.heading_tag: str | None = None
        self.items: list[tuple[str, str]] = []   # (kind, text) kind = 'h' | 'p'

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self.cur)).strip()
        self.cur = []
        if text:
            self.items.append(("h" if self.heading_tag else "p", text))

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            self.heading_tag = tag if tag in _HEADINGS else None

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            self.heading_tag = None

    def handle_data(self, data):
        if not self.skip:
            self.cur.append(data)


def html_to_clauses(html: str) -> list[tuple[str, str]]:
    """Return [(heading, verbatim text), ...] with boilerplate removed."""
    p = _Blocks()
    p.feed(html)
    p._flush()
    out: list[tuple[str, str]] = []
    heading, buf = "overview", ""
    seen: set[str] = set()

    def emit() -> None:
        nonlocal buf
        t = buf.strip()
        if len(t) >= MIN_CLAUSE_CHARS and t not in seen:
            seen.add(t)
            out.append((heading, t))
        buf = ""

    for kind, text in p.items:
        if kind == "h":
            emit()
            heading = text[:80]
            continue
        if _NOISE_HEADING.search(heading):
            continue
        if _BOILER.search(text) or (len(text) < 25 and not re.search(r"\d", text)):
            continue                                  # short numeric cells (rates, limits) are kept
        if len(text) > MAX_CLAUSE_CHARS:           # long paragraph: split on sentence ends
            emit()
            for part in re.split(r"(?<=[.;:])\s+(?=[A-Z0-9(])", text):
                if buf and len(buf) + len(part) > MAX_CLAUSE_CHARS:
                    emit()
                buf = f"{buf} {part}".strip()
            emit()
            continue
        if buf and len(buf) + len(text) > MAX_CLAUSE_CHARS:
            emit()
        buf = f"{buf}\n{text}".strip() if buf else text
    emit()
    return out[:MAX_CLAUSES_PER_PAGE]


def fetch_any(url: str) -> tuple[str, bytes | str]:
    """Return ('html', text) or ('pdf', bytes)."""
    last: Exception | None = None
    for attempt in range(2):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT_S)
            if r.status_code == 200 and r.content:
                if "pdf" in r.headers.get("content-type", "").lower() or r.content[:4] == b"%PDF":
                    return "pdf", r.content
                r.encoding = r.encoding or "utf-8"
                return "html", r.text
            last = RuntimeError(f"HTTP {r.status_code}")
        except requests.RequestException as exc:
            last = exc
        time.sleep(2)
    raise RuntimeError(f"fetch failed for {url}: {last}")


def fetch_html(url: str) -> str:
    kind, data = fetch_any(url)
    return data if kind == "html" else ""


def pdf_to_clauses(data: bytes, max_pages: int = 30, max_clauses: int = 120) -> list[tuple[str, str]]:
    """Verbatim sentence-grouped chunks per page of a text PDF (no OCR)."""
    import pymupdf

    out: list[tuple[str, str]] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for n, page in enumerate(list(doc)[:max_pages], 1):
            text = re.sub(r"[ \t]+", " ", page.get_text("text"))
            text = re.sub(r"\n(?=[a-z(])", " ", text)      # re-join wrapped lines
            text = re.sub(r"\s*\n\s*", " ", text).strip()
            buf = ""
            for part in re.split(r"(?<=[.;:])\s+(?=[A-Z0-9(])", text):
                if buf and len(buf) + len(part) > MAX_CLAUSE_CHARS:
                    if len(buf) >= MIN_CLAUSE_CHARS and not _BOILER.search(buf):
                        out.append((f"page {n}", buf))
                    buf = ""
                buf = f"{buf} {part}".strip()
            if len(buf) >= MIN_CLAUSE_CHARS and not _BOILER.search(buf):
                out.append((f"page {n}", buf))
            if len(out) >= max_clauses:
                break
    return out[:max_clauses]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:36] or "section"


def build_scheme_file(scheme_code: str, urls: list[str]) -> list[dict]:
    fetched = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows: list[dict] = []
    for url in urls:
        try:
            kind, data = fetch_any(url)
        except RuntimeError as exc:
            print(f"  GAP {url}: {exc}")
            continue
        domain = urlparse(url).netloc.lower()
        pieces = pdf_to_clauses(data) if kind == "pdf" else html_to_clauses(data)
        if not pieces:
            print(f"  GAP {url}: no extractable text")
        for i, (heading, text) in enumerate(pieces, 1):
            rows.append({
                "scheme_code": scheme_code,
                "clause_ref": f"{_slug(urlparse(url).path.rsplit('/', 1)[-1] or 'home')[:20]}.{_slug(heading)}.{i}",
                "clause_text": text,
                "source_url": url,
                "source_domain": domain,
                "fetched_at": fetched,
            })
        time.sleep(DELAY_S)
    return rows


def main() -> None:
    if len(sys.argv) > 2 and sys.argv[1] == "--inspect":
        kind, data = fetch_any(sys.argv[2])
        for h, t in (pdf_to_clauses(data) if kind == "pdf" else html_to_clauses(data)):
            print(f"[{h}] {t}\n")
        return
    by_scheme: dict[str, list[str]] = {}
    for code, url in SOURCES:
        by_scheme.setdefault(code, []).append(url)
    for code, urls in by_scheme.items():
        rows = build_scheme_file(code, urls)
        if rows:
            (KB_DIR / f"{code.lower()}.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{code}: {len(rows)} clauses")


if __name__ == "__main__":
    main()
