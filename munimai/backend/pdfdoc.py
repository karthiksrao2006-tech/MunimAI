"""
PDF and PNG rendering for answers, the bank file and the loan application.

PyMuPDF's HTML engine shapes Kannada and Devanagari correctly (ReportLab does
not), using the Noto fonts shipped in assets/fonts. Latin, Kannada and
Devanagari runs are wrapped in separate spans so each gets the right font.
"""

from __future__ import annotations

import html
import io
import re
from pathlib import Path
from typing import Optional

import pymupdf

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

CSS = """
@font-face{font-family:NS;src:url(NotoSans-Regular.ttf);}
@font-face{font-family:NS;font-weight:bold;src:url(NotoSans-Bold.ttf);}
@font-face{font-family:NK;src:url(NotoSansKannada-Regular.ttf);}
@font-face{font-family:NK;font-weight:bold;src:url(NotoSansKannada-Bold.ttf);}
@font-face{font-family:ND;src:url(NotoSansDevanagari-Regular.ttf);}
@font-face{font-family:ND;font-weight:bold;src:url(NotoSansDevanagari-Bold.ttf);}
body{font-family:NS;font-size:10.5pt;color:#0E2A33;line-height:1.45;}
.kn{font-family:NK;} .hi{font-family:ND;}
h1{font-size:22pt;color:#075A57;margin:0 0 4pt 0;}
h2{font-size:13pt;color:#075A57;margin:16pt 0 5pt 0;border-bottom:1pt solid #D3E1DE;padding-bottom:2pt;}
p{margin:3pt 0 6pt 0;}
table{border-collapse:collapse;width:100%;margin:4pt 0 8pt 0;}
th{background:#DDF1EE;text-align:left;padding:4pt 6pt;font-size:9.5pt;}
td{padding:4pt 6pt;border-bottom:0.5pt solid #D3E1DE;vertical-align:top;font-size:10pt;}
td.r,th.r{text-align:right;}
.note{background:#FFF1D6;border:0.5pt solid #F5A524;padding:6pt 8pt;margin:8pt 0;font-size:9.5pt;}
.muted{color:#4A6068;font-size:9.5pt;}
.blank{color:#B8400D;}
.ok{color:#1F8F4E;} .no{color:#B8400D;}
.sig{margin-top:30pt;}
"""

_KN = re.compile(r"[ಀ-೿]+")
_HI = re.compile(r"[ऀ-ॿ]+")


def t(text: Optional[str]) -> str:
    """HTML-escape and wrap Kannada/Devanagari runs so they use the right font."""
    s = html.escape(text or "")
    s = _KN.sub(lambda m: f'<span class="kn">{m.group(0)}</span>', s)
    return _HI.sub(lambda m: f'<span class="hi">{m.group(0)}</span>', s).replace("\n", "<br>")


def inr(paise_or_rupees: float, *, paise: bool = True) -> str:
    v = paise_or_rupees / 100 if paise else paise_or_rupees
    whole, frac = f"{abs(v):.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
    s = f"{head},{tail}" if head else tail
    return f"{'-' if v < 0 else ''}Rs {s}.{frac}"


def html_to_pdf(body: str) -> bytes:
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    media = pymupdf.paper_rect("a4")
    where = media + (46, 46, -46, -58)
    story = pymupdf.Story(html=f"<html><body>{body}</body></html>", user_css=CSS,
                          archive=pymupdf.Archive(str(FONT_DIR)))
    more = 1
    while more:
        dev = writer.begin_page(media)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return buf.getvalue()


def stamp_footer(pdf: bytes, footer: str) -> bytes:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    n = len(doc)
    for i, page in enumerate(doc, 1):
        r = page.rect
        page.insert_text((46, r.height - 28), footer, fontsize=7.5, color=(0.29, 0.38, 0.41))
        page.insert_text((46, r.height - 17), f"Page {i} of {n}", fontsize=7.5, color=(0.29, 0.38, 0.41))
    out = doc.tobytes()
    doc.close()
    return out


def append_originals(pdf: bytes, files: list[tuple[str, str]]) -> bytes:
    """Append original uploads (PDF pages or images) as an appendix, each page
    titled with its file name. files = [(path, display_name)]."""
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    for path, name in files:
        p = Path(path)
        if not p.exists():
            continue
        if p.suffix.lower() == ".pdf":
            src = pymupdf.open(str(p))
            start = len(doc)
            doc.insert_pdf(src)
            for i in range(start, len(doc)):
                doc[i].insert_text((46, 22), f"Appendix: {name}", fontsize=8, color=(0.03, 0.35, 0.34))
            src.close()
        else:
            try:
                page = doc.new_page(width=595, height=842)
                page.insert_text((46, 34), f"Appendix: {name}", fontsize=9, color=(0.03, 0.35, 0.34))
                page.insert_image(pymupdf.Rect(46, 46, 549, 790), filename=str(p), keep_proportion=True)
            except Exception:
                doc.delete_page(len(doc) - 1)
    out = doc.tobytes()
    doc.close()
    return out


def pdf_first_page_png(pdf: bytes, dpi: int = 150) -> bytes:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    png = doc[0].get_pixmap(dpi=dpi).tobytes("png")
    doc.close()
    return png
