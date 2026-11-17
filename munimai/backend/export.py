"""Turn any assistant reply into a PDF or a PNG card the user can save or share."""

from __future__ import annotations

import time

from .pdfdoc import html_to_pdf, pdf_first_page_png, stamp_footer, t
from .voice import split_sources

FOOTER = "Prepared by SaralcreditAI. Information only, not legal, tax or banking advice."


def reply_pdf(question: str, reply: str, reply_en: str = "", language: str = "en") -> bytes:
    body, sources = split_sources(reply)
    parts = [f"<h1>SaralcreditAI answer</h1><p class='muted'>{time.strftime('%d %B %Y, %H:%M')}</p>"]
    if question:
        parts.append(f"<h2>Your question</h2><p>{t(question)}</p>")
    parts.append(f"<h2>Answer</h2><p>{t(body)}</p>")
    if language != "en" and reply_en:
        parts.append(f"<h2>English version</h2><p>{t(split_sources(reply_en)[0])}</p>")
    if sources:
        parts.append(f"<h2>Sources</h2><p class='muted'>{t(sources)}</p>")
    parts.append("<div class='note'>Please verify important details with the official source or your bank "
                 "before acting on them.</div>")
    return stamp_footer(html_to_pdf("".join(parts)), FOOTER)


def reply_png(question: str, reply: str, reply_en: str = "", language: str = "en") -> bytes:
    return pdf_first_page_png(reply_pdf(question, reply, reply_en, language))
