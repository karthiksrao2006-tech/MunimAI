"""
The user's own documents: stored, classified, searchable.

Privacy rules baked in:
- Aadhaar and PAN numbers are masked in everything stored in the database
  (text, facts, summaries). The original file is kept on disk and only leaves
  the machine inside the bank file if the user ticks it.
- GSTIN and Udyam numbers are business identifiers; they are kept, validated by
  pattern, and may auto-fill the business profile.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Optional

import psycopg

UPLOAD_ROOT = Path(__file__).resolve().parent.parent / "uploads"

DDL = """
CREATE TABLE IF NOT EXISTS documents (
    id          BIGSERIAL PRIMARY KEY,
    business_id BIGINT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    filename    TEXT   NOT NULL,
    path        TEXT   NOT NULL,
    kind        TEXT   NOT NULL,
    text        TEXT   NOT NULL DEFAULT '',
    summary     TEXT,
    facts       JSONB  NOT NULL DEFAULT '{}'::jsonb,
    include_in_bank_file BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS documents_business_idx ON documents (business_id, created_at DESC);
"""

_GSTIN = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
_UDYAM = re.compile(r"\b(UDYAM-[A-Z]{2}-\d{2}-\d{7})\b", re.I)
_PAN = re.compile(r"\b([A-Z]{5})(\d{4})([A-Z])\b")
_AADHAAR = re.compile(r"\b(\d{4})\s?(\d{4})\s?(\d{4})\b")
_IFSC = re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")

KINDS = ("invoice", "udyam_certificate", "gst_certificate", "id_proof", "bank_statement", "other")


def ensure_table(conn: psycopg.Connection) -> None:
    conn.execute(DDL)
    conn.commit()


def mask_ids(text: str) -> str:
    """Mask Aadhaar and PAN numbers; leave everything else intact."""
    text = _PAN.sub(lambda m: f"{m.group(1)[:2]}XXX{m.group(2)[:0]}XXXX{m.group(3)}", text)
    return _AADHAAR.sub(lambda m: f"XXXX XXXX {m.group(3)}", text)


def classify(text: str, filename: str = "") -> str:
    t = f"{filename} {text}".lower()
    if "udyam" in t and ("certificate" in t or "registration" in t):
        return "udyam_certificate"
    if "registration certificate" in t and ("goods and services tax" in t or "gstin" in t):
        return "gst_certificate"
    if any(k in t for k in ("aadhaar", "uidai", "permanent account number", "income tax department")):
        return "id_proof"
    if any(k in t for k in ("account statement", "statement of account", "opening balance", "closing balance", "ifsc")):
        return "bank_statement"
    if any(k in t for k in ("invoice", "bill to", "tax invoice", "cash memo", "total payable", "grand total")):
        return "invoice"
    return "other"


def extract_facts(text: str) -> dict:
    facts: dict = {}
    if m := _GSTIN.search(text):
        facts["gstin"] = m.group(1)
    if m := _UDYAM.search(text):
        facts["udyam"] = m.group(1).upper()
    if m := _PAN.search(text):
        facts["pan_masked"] = f"{m.group(1)[:2]}XXX XXXX{m.group(3)}"
    if "aadhaar" in text.lower() or "uidai" in text.lower():
        if m := _AADHAAR.search(text):
            facts["aadhaar_last4"] = m.group(3)
    if m := _IFSC.search(text):
        facts["ifsc"] = m.group(1)
    return facts


def save_upload(conn: psycopg.Connection, business_id: int, filename: str,
                data: bytes, text: str) -> dict:
    """Store the file on disk and a masked, searchable record in the database."""
    kind = classify(text, filename)
    facts = extract_facts(text)
    ext = Path(filename).suffix.lower()[:8] or ".bin"
    folder = UPLOAD_ROOT / str(business_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}{ext}"
    path.write_bytes(data)
    row = conn.execute(
        """INSERT INTO documents (business_id, filename, path, kind, text, facts)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
        (business_id, Path(filename).name[:120], str(path), kind, mask_ids(text)[:60000],
         json.dumps(facts))).fetchone()
    # fill the profile from official business identifiers, only when empty
    if "gstin" in facts:
        conn.execute("UPDATE businesses SET gstin=%s WHERE id=%s AND gstin IS NULL", (facts["gstin"], business_id))
    if "udyam" in facts:
        conn.execute("UPDATE businesses SET udyam_number=%s WHERE id=%s AND udyam_number IS NULL", (facts["udyam"], business_id))
    conn.commit()
    return {"id": row[0], "kind": kind, "facts": facts, "filename": filename}


def set_summary(conn: psycopg.Connection, doc_id: int, summary: str) -> None:
    conn.execute("UPDATE documents SET summary=%s WHERE id=%s", (mask_ids(summary)[:600], doc_id))
    conn.commit()


def list_docs(conn: psycopg.Connection, business_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, filename, kind, summary, facts, include_in_bank_file, created_at
           FROM documents WHERE business_id=%s ORDER BY id DESC""", (business_id,)).fetchall()
    return [{"id": r[0], "filename": r[1], "kind": r[2], "summary": r[3], "facts": r[4],
             "include": r[5], "created": r[6].isoformat()} for r in rows]


def set_include(conn: psycopg.Connection, business_id: int, doc_id: int, include: bool) -> None:
    conn.execute("UPDATE documents SET include_in_bank_file=%s WHERE id=%s AND business_id=%s",
                 (include, doc_id, business_id))
    conn.commit()


_WORD = re.compile(r"[A-Za-z0-9]{3,}")
_STOP = {"the", "and", "for", "what", "how", "much", "does", "this", "that", "with", "from",
         "have", "your", "you", "are", "was", "who", "which", "can", "tell", "give", "show",
         "about", "please", "mine", "my"}


def _terms(s: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(s)} - _STOP


def retrieve(conn: psycopg.Connection, business_id: int, question: str,
             k: int = 4, chunk_chars: int = 900) -> list[dict]:
    """Keyword-scored chunks from the user's documents (no embedding model)."""
    q = _terms(question)
    if not q:
        return []
    out = []
    for doc_id, filename, kind, text in conn.execute(
            "SELECT id, filename, kind, text FROM documents WHERE business_id=%s", (business_id,)).fetchall():
        for i in range(0, max(len(text), 1), chunk_chars):
            chunk = text[i:i + chunk_chars + 150]
            score = len(q & _terms(chunk)) + (1 if kind != "other" and q & _terms(kind.replace("_", " ")) else 0)
            if score:
                out.append({"doc_id": doc_id, "filename": filename, "kind": kind, "text": chunk, "score": score})
    out.sort(key=lambda c: -c["score"])
    return out[:k]


def has_documents(conn: psycopg.Connection, business_id: int) -> bool:
    return conn.execute("SELECT 1 FROM documents WHERE business_id=%s LIMIT 1", (business_id,)).fetchone() is not None


def get_path(conn: psycopg.Connection, business_id: int, doc_id: int) -> Optional[tuple[str, str]]:
    r = conn.execute("SELECT path, filename FROM documents WHERE id=%s AND business_id=%s", (doc_id, business_id)).fetchone()
    return (r[0], r[1]) if r else None
