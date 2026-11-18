"""
Verifiable bank file.

When the owner generates a bank file or a loan application, the books behind
it are sealed: every ledger entry goes into a SHA-256 hash chain, the proofs on
file are hashed, and the result (the "root") is printed on the PDF together
with a QR code. A bank officer scans the QR and the verify page:

  1. confirms the sealed record is internally consistent (the chain recomputes),
  2. shows the totals the PDF should match,
  3. says whether the owner's live books have changed since sealing,
  4. lets the officer upload the PDF they were given; it must be byte-identical
     to the file SaralcreditAI issued.

Honest limit: the notary is this server. A deployment would also anchor the
root with an outside timestamp authority so that even the operator cannot
rewrite history. The verify page says so in plain words.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg
import pymupdf
import segno

_GENESIS = hashlib.sha256(b"saralcredit-seal-v1").hexdigest()


def public_base() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8088").rstrip("/")


def _canon(row: tuple) -> list:
    eid, d, direction, amt, cat, cp, gst, src = row
    return [int(eid), str(d), direction, int(amt), cat or "", cp or "", -1 if gst is None else int(gst), src or ""]


def _ledger_rows(conn: psycopg.Connection, bid: int) -> list[list]:
    rows = conn.execute(
        """SELECT id, entry_date, direction, amount_paise, category, counterparty, gst_rate_bp, source
           FROM ledger_entries WHERE business_id=%s AND needs_review=FALSE ORDER BY id""", (bid,)).fetchall()
    return [_canon(r) for r in rows]


def chain(entries: list[list]) -> str:
    h = _GENESIS
    for e in entries:
        h = hashlib.sha256((h + json.dumps(e, separators=(",", ":"), ensure_ascii=False)).encode()).hexdigest()
    return h


def _docs(conn: psycopg.Connection, bid: int) -> list[list]:
    out = []
    try:
        rows = conn.execute("SELECT id, filename, path FROM documents WHERE business_id=%s ORDER BY id", (bid,)).fetchall()
    except psycopg.Error:
        conn.rollback()
        return out
    for did, name, path in rows:
        p = Path(path) if path else None
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if p and p.exists() else "missing"
        out.append([int(did), name, digest])
    return out


def _docs_digest(docs: list[list]) -> str:
    return hashlib.sha256(json.dumps(docs, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _root(head: str, docs_digest: str, bid: int, sealed_at: str, n: int) -> str:
    return hashlib.sha256(f"{head}|{docs_digest}|{bid}|{sealed_at}|{n}".encode()).hexdigest()


@dataclass
class Seal:
    token: str
    root: str
    entries: int
    sealed_at: datetime

    @property
    def url(self) -> str:
        return f"{public_base()}/verify/{self.token}?h={self.root[:16]}"


def create_seal(conn: psycopg.Connection, bid: int, kind: str) -> Seal:
    name = conn.execute("SELECT display_name FROM businesses WHERE id=%s", (bid,)).fetchone()[0]
    entries = _ledger_rows(conn, bid)
    docs = _docs(conn, bid)
    head, dd = chain(entries), _docs_digest(docs)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    root = _root(head, dd, bid, now.isoformat(), len(entries))
    token = secrets.token_urlsafe(18)
    tin = sum(e[3] for e in entries if e[2] == "in")
    tout = sum(e[3] for e in entries if e[2] == "out")
    dates = [e[1] for e in entries]
    conn.execute(
        """INSERT INTO seals (token, business_id, kind, sealed_at, entries, first_date, last_date,
                              total_in_paise, total_out_paise, ledger_head, docs_digest, root, snapshot)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (token, bid, kind, now, len(entries), min(dates) if dates else None, max(dates) if dates else None,
         tin, tout, head, dd, root, json.dumps({"business": name, "entries": entries, "docs": docs}, ensure_ascii=False)))
    conn.commit()
    return Seal(token, root, len(entries), now)


def qr_png(url: str) -> bytes:
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=4, border=1, dark="#075A57")
    return buf.getvalue()


def stamp_seal(pdf: bytes, seal: Seal) -> bytes:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    grey = (0.29, 0.38, 0.41)
    first = doc[0]
    first.insert_image(pymupdf.Rect(491, 10, 549, 68), stream=qr_png(seal.url))
    first.insert_text((372, 30), "Books sealed. Scan to verify", fontsize=7.5, color=(0.03, 0.35, 0.34))
    first.insert_text((372, 41), f"Seal {seal.root[:16]}", fontsize=7.5, color=grey)
    first.insert_text((372, 52), f"{seal.entries} entries, {seal.sealed_at:%d %b %Y %H:%M} UTC", fontsize=7.5, color=grey)
    for page in doc:
        page.insert_text((330, page.rect.height - 17), f"Seal {seal.root[:24]}", fontsize=7.5, color=grey)
    out = doc.tobytes()
    doc.close()
    return out


def record_pdf_hash(conn: psycopg.Connection, token: str, pdf: bytes) -> None:
    conn.execute("UPDATE seals SET pdf_sha256=%s WHERE token=%s", (hashlib.sha256(pdf).hexdigest(), token))
    conn.commit()


def seal_pdf(conn: psycopg.Connection, bid: int, pdf: bytes, kind: str) -> bytes:
    """Seal the books behind a PDF and return the PDF with the QR and seal id."""
    seal = create_seal(conn, bid, kind)
    out = stamp_seal(pdf, seal)
    record_pdf_hash(conn, seal.token, out)
    return out


def verify(conn: psycopg.Connection, token: str) -> Optional[dict]:
    r = conn.execute(
        """SELECT business_id, kind, sealed_at, entries, first_date, last_date, total_in_paise, total_out_paise,
                  ledger_head, docs_digest, root, snapshot, pdf_sha256 FROM seals WHERE token=%s""", (token,)).fetchone()
    if not r:
        return None
    (bid, kind, sealed_at, n, d0, d1, tin, tout, head, dd, root, snap, pdf_hash) = r
    entries, docs = snap["entries"], snap["docs"]
    iso = sealed_at.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    intact = (chain(entries) == head and _docs_digest(docs) == dd and _root(head, dd, bid, iso, n) == root)
    live = {e[0]: e for e in _ledger_rows(conn, bid)}
    sealed = {e[0]: e for e in entries}
    added = [i for i in live if i not in sealed]
    removed = [i for i in sealed if i not in live]
    changed = [i for i in sealed if i in live and live[i] != sealed[i]]
    return {"token": token, "kind": kind, "business": snap.get("business", ""), "sealed_at": sealed_at,
            "entries": n, "first_date": d0, "last_date": d1, "total_in_paise": int(tin), "total_out_paise": int(tout),
            "docs": len(docs), "root": root, "intact": intact, "pdf_sha256": pdf_hash,
            "live": {"added": len(added), "removed": len(removed), "changed": len(changed)}}


def pdf_matches(conn: psycopg.Connection, token: str, data: bytes) -> bool:
    row = conn.execute("SELECT pdf_sha256 FROM seals WHERE token=%s", (token,)).fetchone()
    return bool(row and row[0] and hashlib.sha256(data).hexdigest() == row[0])


def _inr(paise: int) -> str:
    whole = f"{abs(paise) / 100:,.2f}"
    return f"Rs {whole}"


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Verify a SaralcreditAI file</title><style>
:root{{--teal:#0B7A75;--deep:#075A57;--paper:#F2F7F6;--ink:#0E2A33;--mut:#4A6068;--ok:#1F8F4E;--bad:#B8400D;--gold:#F5A524}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 "Segoe UI",system-ui,sans-serif}}
main{{max-width:640px;margin:0 auto;padding:28px 16px 48px}}
h1{{font-size:1.5rem;margin:0 0 4px;color:var(--deep)}}.sub{{color:var(--mut);margin:0 0 20px}}
.card{{background:#fff;border:1px solid #D3E1DE;border-radius:14px;padding:16px 18px;margin:0 0 14px}}
.badge{{display:inline-block;padding:3px 10px;border-radius:99px;font-weight:600;font-size:.85rem}}
.ok{{background:#DFF3E7;color:var(--ok)}}.bad{{background:#FBE3D6;color:var(--bad)}}.warn{{background:#FFF1D6;color:#8A5A00}}
table{{width:100%;border-collapse:collapse}}td{{padding:6px 0;border-bottom:1px solid #E6EFED;vertical-align:top}}
td:first-child{{color:var(--mut);width:46%}}code{{font-size:.8rem;word-break:break-all}}
button{{background:var(--teal);color:#fff;border:0;border-radius:10px;padding:10px 16px;font-size:1rem;cursor:pointer}}
.note{{color:var(--mut);font-size:.85rem}}
</style></head><body><main>
<h1>Verify a SaralcreditAI file</h1><p class="sub">Check that the bank file you were given matches the books it was sealed from.</p>
{body}
<p class="note">How this works: when the owner creates the file, SaralcreditAI chains every ledger entry and every proof on file into one
fingerprint (the seal). This page recomputes it. The notary here is this server; a production deployment would also anchor the seal with
an outside timestamp authority. This is a draft prepared for the owner. It is not a bank decision.</p>
</main></body></html>"""


def render(v: Optional[dict], token: str, check: Optional[bool] = None) -> str:
    if v is None:
        return _PAGE.format(body='<div class="card"><span class="badge bad">Not found</span>'
                                 '<p>No sealed file matches this code. Ask the owner to generate the file again.</p></div>')
    live = v["live"]
    drift = live["added"] + live["removed"] + live["changed"]
    seal_badge = ('<span class="badge ok">Seal intact</span>' if v["intact"]
                  else '<span class="badge bad">Seal record does not recompute</span>')
    if drift == 0:
        live_txt = '<span class="badge ok">Books unchanged since sealing</span>'
    else:
        parts = [f"{live['added']} added" if live["added"] else "", f"{live['changed']} changed" if live["changed"] else "",
                 f"{live['removed']} removed" if live["removed"] else ""]
        live_txt = ('<span class="badge warn">Owner\'s live books differ: ' + ", ".join(p for p in parts if p) +
                    '</span><p class="note">The file still matches what was sealed. Later changes are shown here, not hidden.</p>')
    chk = ""
    if check is True:
        chk = '<p><span class="badge ok">This PDF is exactly the file SaralcreditAI issued</span></p>'
    elif check is False:
        chk = '<p><span class="badge bad">This PDF does NOT match the issued file. It may have been edited.</span></p>'
    period = f"{v['first_date']} to {v['last_date']}" if v["first_date"] else "no entries"
    body = f"""<div class="card">{seal_badge} {live_txt}
<table>
<tr><td>Business</td><td>{html.escape(v['business'])}</td></tr>
<tr><td>File type</td><td>{'Loan application' if v['kind'] == 'application' else 'Bank file'}</td></tr>
<tr><td>Sealed at</td><td>{v['sealed_at']:%d %B %Y, %H:%M} UTC</td></tr>
<tr><td>Ledger entries sealed</td><td>{v['entries']} ({period})</td></tr>
<tr><td>Money in (total)</td><td>{_inr(v['total_in_paise'])}</td></tr>
<tr><td>Money out (total)</td><td>{_inr(v['total_out_paise'])}</td></tr>
<tr><td>Proof documents sealed</td><td>{v['docs']}</td></tr>
<tr><td>Seal</td><td><code>{v['root']}</code></td></tr></table>
<p class="note">The totals above should match the figures in the PDF's income table. The seal should start with the characters printed at the top of its first page.</p></div>
<div class="card"><b>Check the PDF you received</b>
<form method="post" action="/verify/{html.escape(token)}/check" enctype="multipart/form-data" style="margin-top:8px">
<input type="file" name="file" accept="application/pdf" required> <button type="submit">Check this file</button></form>{chk}</div>"""
    return _PAGE.format(body=body)
