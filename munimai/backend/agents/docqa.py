"""
Answer questions about the user's own uploaded documents.

The model may only use the excerpts it is given. If they do not contain the
answer it must say so (found=false) so the caller falls through to the web.
"""

from __future__ import annotations

from typing import Optional

import psycopg

from .. import docstore
from ..llm import LLM
from ..trace import Trace

_SYSTEM = (
    "You answer questions about a business owner's own documents. Use ONLY the "
    "document excerpts provided. Quote exact figures, names and dates as they "
    "appear. If the excerpts do not contain the answer, set found to false and "
    "say what is missing. Never guess a number. Aadhaar and PAN numbers are "
    "masked in the excerpts; never try to reconstruct them."
)


def answer(conn: psycopg.Connection, llm: LLM, business_id: int, question: str,
           *, trace: Optional[Trace] = None) -> Optional[tuple[str, list[str]]]:
    """Returns (answer, [filenames used]) or None when the documents don't help."""
    chunks = docstore.retrieve(conn, business_id, question)
    if trace:
        trace.emit("docqa", "tool_call", {"chunks": len(chunks),
                                          "files": sorted({c["filename"] for c in chunks})},
                   tool_name="docstore.retrieve")
    if not chunks:
        return None
    excerpts = "\n\n".join(f"[{c['filename']} | {c['kind']}]\n{c['text']}" for c in chunks)
    parsed = llm.json_call(
        _SYSTEM, f"Question: {question}\n\nDocument excerpts:\n{excerpts}",
        schema_hint='{"answer": string, "found": boolean}', trace=trace, agent="docqa", max_tokens=350)
    if not parsed.get("found"):
        return None
    text = str(parsed.get("answer", "")).strip()
    if not text:
        return None
    return text, sorted({c["filename"] for c in chunks})
