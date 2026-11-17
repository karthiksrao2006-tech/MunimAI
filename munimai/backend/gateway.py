"""
The WhatsApp gateway.

Receives a webhook, figures out what kind of message it is, routes it to the
right agent, and replies. This is the live-path entry point described in the
pitch: owner on WhatsApp -> router -> agents -> verifier -> reply.

Message type -> path:
    text  -> Scheme agent (treated as an eligibility/loan question)
    image -> Ledger agent (a bill photo -> a ledger entry)
    audio -> Bhashini ASR -> text -> same path as text

Every webhook event is deduplicated on wa_message_id (Meta retries delivery;
the messages table's UNIQUE constraint makes a retry a no-op, not a double
reply) and is fully traced.

Run:  uvicorn backend.gateway:app --host 0.0.0.0 --port 8088
Needs a public HTTPS URL in front of it (Cloudflare Tunnel / ngrok) for Meta's
webhook to reach it.
"""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path
from typing import Optional

import psycopg
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse

from .agents.ledger import LedgerAgent
from .agents.scheme import SchemeAgent
from .agents.verifier import Verifier
from . import docstore
from . import export as _export
from .agents import general as _general
from .agents.application import BANKS, SCHEMES, AppRequest, ApplicationAgent
from .agents.dossier import _KIND_LABEL, DossierAgent
from .router import CANNED, classify
from .bhashini_client import BhashiniClient, BhashiniConfigError
from .config import DBSettings, LLMSettings, WhatsAppSettings
from .llm import LLM
from .ocr import RemoteEasyOCR
from .seeds import send_seed
from .trace import Trace
from .voice import detect_language, iter_clips, localize_reply, speak_clips, to_english
from .whatsapp import InboundMessage, WhatsAppClient

app = FastAPI(title="MunimAI WhatsApp Gateway")

_db = DBSettings.from_env()
_wa_settings = WhatsAppSettings.from_env()
_wa = WhatsAppClient(_wa_settings)
_llm = LLM()                                     # boss — Qwen/GPU, reasoning + citations
_ledger_llm = LLM(LLMSettings.from_env_worker())  # worker — Gemma/CPU, bill extraction only
_ocr = RemoteEasyOCR()
try:
    _bhashini: Optional[BhashiniClient] = BhashiniClient()
except BhashiniConfigError:
    _bhashini = None  # voice messages degrade to "please type instead"


def _to_wav(audio: bytes) -> bytes:
    """Bhashini ASR accepts WAV only; browsers record WebM/Opus and WhatsApp
    sends OGG/Opus. Re-encode anything else to 16 kHz mono WAV."""
    if audio[:4] == b"RIFF":
        return audio
    import subprocess

    import imageio_ffmpeg

    proc = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", "pipe:0",
         "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"],
        input=audio, capture_output=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        raise ValueError("could not decode audio: " + proc.stderr.decode(errors="replace")[:200])
    return proc.stdout


def _get_conn() -> psycopg.Connection:
    return psycopg.connect(_db.conninfo)


try:
    with psycopg.connect(_db.conninfo) as _c:
        docstore.ensure_table(_c)
except Exception:  # the app still starts; document features report the error when used
    pass

_FILES: dict[str, tuple[bytes, str, str]] = {}   # token -> (bytes, filename, mime)
_LAST: dict[str, dict] = {}                      # browser id -> last turn, for "save this as a PDF"


def _stash(data: bytes, name: str, mime: str) -> str:
    import uuid as _uuid

    tok = _uuid.uuid4().hex
    _FILES[tok] = (data, name, mime)
    while len(_FILES) > 40:
        _FILES.pop(next(iter(_FILES)))
    return tok


def _get_or_create_business(conn: psycopg.Connection, wa_number: str) -> int:
    row = conn.execute(
        "SELECT id FROM businesses WHERE whatsapp_phone=%s", (f"+{wa_number}",)
    ).fetchone()
    if row:
        return row[0]
    bid = conn.execute(
        "INSERT INTO businesses (whatsapp_phone, display_name, is_synthetic)"
        " VALUES (%s,%s,FALSE) RETURNING id",
        (f"+{wa_number}", f"WhatsApp user {wa_number[-4:]}"),
    ).fetchone()[0]
    conn.commit()
    return bid


@app.get("/webhook")
def verify_webhook(request: Request) -> Response:
    """Meta's one-time webhook verification handshake."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge", "")
    if mode == "subscribe" and token == _wa_settings.verify_token:
        return PlainTextResponse(challenge)
    return Response(status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request) -> dict:
    body = await request.json()

    # Debug trail: dump every raw webhook (messages AND status callbacks —
    # sent/delivered/read/failed) so delivery problems are diagnosable instead
    # of guessed at. Remove or gate behind an env flag once past the demo.
    try:
        import json as _json
        with open(Path(__file__).parent.parent / "webhook_raw.jsonl", "a", encoding="utf-8") as _f:
            _f.write(_json.dumps(body, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[gateway] raw log failed: {exc}")

    messages = WhatsAppClient.parse_webhook(body)
    for msg in messages:
        try:
            _handle_message(msg)
        except Exception as exc:  # a bad message must never take down the gateway
            print(f"[gateway] error handling {msg.wa_message_id}: {exc}")
    return {"status": "ok"}


def _handle_message(msg: InboundMessage) -> None:
    conn = _get_conn()
    try:
        business_id = _get_or_create_business(conn, msg.from_number)

        # dedupe at the DB level: a Meta retry hits the UNIQUE constraint and
        # this insert becomes a no-op via ON CONFLICT, so we return early.
        row = conn.execute(
            "INSERT INTO messages (business_id, direction, msg_type, body, wa_message_id)"
            " VALUES (%s,'in',%s,%s,%s) ON CONFLICT (wa_message_id) DO NOTHING RETURNING id",
            (business_id, msg.msg_type, msg.text, msg.wa_message_id),
        ).fetchone()
        conn.commit()
        if row is None:
            return  # already processed this wamid

        trace = Trace(business_id=business_id)
        trace.attach_db(conn)
        trace.emit("router", "message_in",
                   {"type": msg.msg_type, "from": msg.from_number})

        question: Optional[str] = None
        reply: str

        if msg.msg_type == "text":
            question = msg.text

        elif msg.msg_type == "audio":
            question = _transcribe_voice(msg, conn, trace)
            if question is None:
                reply = ("Voice messages need the Bhashini service, which isn't "
                         "configured right now. Please type your question instead.")
                _reply(msg.from_number, reply, business_id, conn, trace)
                return

        elif msg.msg_type == "image":
            reply = _handle_bill_photo(msg, business_id, conn, trace)
            _reply(msg.from_number, reply, business_id, conn, trace)
            return

        else:
            reply = "I can read text, voice notes, and bill photos. Please send one of those."
            _reply(msg.from_number, reply, business_id, conn, trace)
            return

        # text (typed or transcribed) -> scheme eligibility path
        reply = _handle_question(question or "", business_id, conn, trace)
        _reply(msg.from_number, reply, business_id, conn, trace)
    finally:
        conn.close()


def _transcribe_voice(msg: InboundMessage, conn: psycopg.Connection,
                      trace: Trace) -> Optional[str]:
    if _bhashini is None:
        return None
    trace.emit("router", "tool_call", {"media_id": msg.media_id}, tool_name="wa.download")
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        tmp_path = f.name
    try:
        _wa.download_media(msg.media_id, tmp_path)
        audio_bytes = Path(tmp_path).read_bytes()
        # Bhashini ASR expects WAV; WhatsApp sends OGG/Opus. For the demo we
        # try Hindi first — a real build would detect language or ask once.
        text = _bhashini.speech_to_text(_to_wav(audio_bytes), language="hi")
        trace.emit("router", "tool_result", {"transcript": text[:200]},
                  tool_name="bhashini.asr")
        return text
    except Exception as exc:
        trace.emit("router", "error", {"stage": "asr", "error": str(exc)})
        return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _handle_bill_photo(msg: InboundMessage, business_id: int,
                       conn: psycopg.Connection, trace: Trace) -> str:
    trace.emit("router", "route", {"to": "ledger"})
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        tmp_path = f.name
    try:
        _wa.download_media(msg.media_id, tmp_path)
        text = _ocr.read(tmp_path)
        if not text.strip():
            return ("I couldn't read any text from that photo. Could you "
                    "retake it with better light, closer and flat?")
        entry = LedgerAgent(conn, _ledger_llm).from_text(
            business_id, text, source="bill_photo",
            source_media_id=msg.media_id, trace=trace)
        conn.commit()
        if entry.needs_review:
            return (f"I've recorded a draft entry but need you to check it: "
                    f"{entry.review_reason}. Category: {entry.category}.")
        rupees = entry.amount_paise / 100
        return (f"Recorded: Rs {rupees:,.2f} ({entry.direction}, "
                f"{entry.category})" + (f", GST {entry.gst_rate_bp/100:.0f}%"
                if entry.gst_rate_bp else "") + ". Added to your books.")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _converse(text: str, lang: str, bid: int, conn: psycopg.Connection,
              trace: Trace, speak: bool) -> dict:
    """Any language in -> English reasoning -> same language out (+ speech)."""
    if lang == "auto":
        lang = detect_language(text)
    note = None
    try:
        text_en = to_english(_bhashini, text, lang)
    except Exception as exc:  # translation outage: degrade, don't fail the chat
        trace.emit("router", "error", {"stage": "translate_in", "error": str(exc)})
        text_en, lang, note = text, "en", "Translation is unavailable right now, so I'm answering in English."
    if lang != "en":
        trace.emit("router", "tool_result", {"language": lang, "understood": text_en[:200]},
                   tool_name="bhashini.translate")
    send_seed("question", text_en, lang)
    reply_en = _handle_question(text_en, bid, conn, trace)
    try:
        reply = localize_reply(_bhashini, reply_en, lang)
        clips = speak_clips(_bhashini, reply, lang) if speak else []
    except Exception as exc:
        trace.emit("router", "error", {"stage": "translate_out", "error": str(exc)})
        reply, clips, note = reply_en, [], "I couldn't translate the reply, so here it is in English."
    if note:
        reply = f"{note}\n\n{reply}"
    trace.emit("router", "message_out", {"reply": reply[:300], "language": lang})
    return {"reply": reply, "reply_en": reply_en, "language": lang,
            "understood": text_en, "audio": clips, "trace_id": str(trace.trace_id)}


def _handle_question(question: str, business_id: int,
                     conn: psycopg.Connection, trace: Trace) -> str:
    intent = classify(question)
    trace.emit("router", "route", {"intent": intent})
    if intent in CANNED:
        return CANNED[intent]
    if intent != "eligibility":
        trace.emit("router", "route", {"to": "general"})
        text, source = _general.answer(conn, business_id, question, _llm,
                                       has_docs=docstore.has_documents(conn, business_id), trace=trace)
        trace.emit("router", "tool_result", {"answer_source": source}, tool_name="general")
        return text
    trace.emit("router", "route", {"to": "scheme"})
    answer, claim = SchemeAgent(conn, _llm).answer(business_id, question, trace=trace)
    verdict = Verifier(conn).check(claim)
    if not verdict.passed:
        return ("I can't confirm that yet — " + "; ".join(verdict.reasons)
                + ". Let me find the right source before answering.")
    cited = conn.execute(
        "SELECT clause_ref FROM scheme_clauses WHERE id=ANY(%s)",
        (verdict.valid_clause_ids,),
    ).fetchall()
    refs = ", ".join(r[0] for r in cited)
    return f"{answer.text}\n\n(Sources: {refs})" if refs else answer.text


def _reply(to: str, text: str, business_id: int, conn: psycopg.Connection,
          trace: Trace) -> None:
    wamid = _wa.send_text(to, text)
    conn.execute(
        "INSERT INTO messages (business_id, direction, msg_type, body, wa_message_id)"
        " VALUES (%s,'out','text',%s,%s) ON CONFLICT (wa_message_id) DO NOTHING",
        (business_id, text, wamid),
    )
    conn.commit()
    trace.emit("router", "message_out", {"reply": text[:300]})


# =========================================================================
#  Simulator mode: a web page talks to these endpoints and gets the SAME
#  agent pipeline that WhatsApp would, but with no Meta send. The reply is
#  returned synchronously so a browser can show it right away.
# =========================================================================

from fastapi import UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse

_SIM_PHONE = "+919900000001"      # Lakshmi (demo business)


def _sim_business(conn: psycopg.Connection, munim_id: Optional[str] = None) -> int:
    """The business for this browser. Each browser gets its own real profile,
    keyed by an id it generates once; without an id we fall back to the shared
    synthetic demo business (used only by tests and old clients)."""
    import re as _re

    clean = _re.sub(r"[^A-Za-z0-9-]", "", munim_id or "")[:64]
    if not clean:
        row = conn.execute("SELECT id FROM businesses WHERE whatsapp_phone=%s", (_SIM_PHONE,)).fetchone()
        if row:
            return row[0]
        return conn.execute(
            "INSERT INTO businesses (whatsapp_phone, display_name, language, is_synthetic)"
            " VALUES (%s,%s,'kn',TRUE) RETURNING id", (_SIM_PHONE, "Lakshmi Tailoring Unit"),
        ).fetchone()[0]
    key = f"web:{clean}"
    row = conn.execute("SELECT id FROM businesses WHERE whatsapp_phone=%s", (key,)).fetchone()
    if row:
        return row[0]
    return conn.execute(
        "INSERT INTO businesses (whatsapp_phone, display_name, language, is_synthetic)"
        " VALUES (%s,%s,'kn',FALSE) RETURNING id", (key, "My business"),
    ).fetchone()[0]


@app.get("/sim/profile")
def sim_profile(x_munim_id: Optional[str] = Header(None)) -> dict:
    conn = _get_conn()
    try:
        bid = _sim_business(conn, x_munim_id); conn.commit()
        r = conn.execute("SELECT display_name, gstin, udyam_number FROM businesses WHERE id=%s", (bid,)).fetchone()
        return {"name": r[0], "gstin": r[1], "udyam": r[2], "is_new": r[0] == "My business"}
    finally:
        conn.close()


@app.post("/sim/profile")
def sim_profile_save(body: dict, x_munim_id: Optional[str] = Header(None)) -> dict:
    name = (body.get("name") or "").strip()[:80]
    if not name:
        return {"error": "Enter your business name."}
    gstin = (body.get("gstin") or "").strip().upper()[:15] or None
    conn = _get_conn()
    try:
        bid = _sim_business(conn, x_munim_id)
        conn.execute("UPDATE businesses SET display_name=%s, gstin=%s WHERE id=%s", (name, gstin, bid))
        conn.commit()
        return {"name": name, "gstin": gstin}
    finally:
        conn.close()


@app.get("/sim/status")
def sim_status() -> dict:
    """Health of the two models, for the test console's status strip."""
    return {"boss": _llm.health(), "worker": _ledger_llm.health()}


@app.get("/sim/books")
def sim_books(x_munim_id: Optional[str] = Header(None)) -> dict:
    """Latest ledger entries for the demo business, for the test console."""
    conn = _get_conn()
    try:
        bid = _sim_business(conn, x_munim_id); conn.commit()
        rows = conn.execute(
            """SELECT entry_date, direction, amount_paise, category, counterparty,
                      gst_rate_bp, needs_review
               FROM ledger_entries WHERE business_id=%s ORDER BY id DESC LIMIT 8""",
            (bid,)).fetchall()
        return {"entries": [
            {"date": str(r[0]), "direction": r[1], "amount": int(r[2]) / 100,
             "category": r[3], "counterparty": r[4],
             "gst_pct": (r[5] / 100) if r[5] else None, "review": r[6]}
            for r in rows]}
    finally:
        conn.close()


@app.post("/sim/text")
async def sim_text(body: dict, x_munim_id: Optional[str] = Header(None)) -> dict:
    """Text in any supported language; reply in the same language."""
    text = (body.get("text") or "").strip()
    if not text:
        return {"reply": "(empty message)"}
    conn = _get_conn()
    try:
        bid = _sim_business(conn, x_munim_id); conn.commit()
        trace = Trace(business_id=bid); trace.attach_db(conn)
        trace.emit("router", "message_in", {"type": "text", "from": "sim"})
        return _converse(text, body.get("language", "auto"), bid, conn, trace,
                         bool(body.get("speak")))
    finally:
        conn.close()


def _summarize_doc(doc_id: int, text: str) -> None:
    """Background Gemma-worker job: a two-sentence summary of an upload."""
    try:
        r = _ledger_llm.chat([
            {"role": "system", "content": "Summarise this document in at most two short sentences. "
             "Say what kind of document it is and its key facts. Do not invent anything."},
            {"role": "user", "content": text[:1500]}], max_tokens=90)
        with psycopg.connect(_db.conninfo) as c:
            docstore.set_summary(c, doc_id, r.text.strip())
    except Exception:
        pass


def _pdf_text(path: str, max_pages: int = 3) -> str:
    """Digital PDFs carry a text layer (exact, free). Scanned ones don't, so
    render each page to PNG and send it through the same OCR as a photo."""
    import pymupdf

    parts: list[str] = []
    with pymupdf.open(path) as doc:
        for page in list(doc)[:max_pages]:
            embedded = page.get_text().strip()
            if embedded:
                parts.append(embedded)
                continue
            png = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            png.close()
            try:
                page.get_pixmap(dpi=200).save(png.name)
                parts.append(_ocr.read(png.name))
            finally:
                Path(png.name).unlink(missing_ok=True)
    return "\n".join(parts)


@app.post("/sim/image")
async def sim_image(file: UploadFile = File(...), x_munim_id: Optional[str] = Header(None)) -> dict:
    """Simulated bill upload (photo, PNG/JPEG or PDF). Runs the real Ledger
    pipeline (OCR -> structure)."""
    conn = _get_conn()
    try:
        bid = _sim_business(conn, x_munim_id); conn.commit()
        trace = Trace(business_id=bid); trace.attach_db(conn)
        trace.emit("router", "message_in", {"type": "image", "from": "sim"})
        with tempfile.NamedTemporaryFile(
            suffix=Path(file.filename or "bill.jpg").suffix or ".jpg",
            delete=False,
        ) as f:
            f.write(await file.read()); tmp = f.name
        try:
            trace.emit("router", "route", {"to": "ledger"})
            text = _pdf_text(tmp) if tmp.lower().endswith(".pdf") else _ocr.read(tmp)
            doc = None
            if text.strip():
                doc = docstore.save_upload(conn, bid, file.filename or "upload", Path(tmp).read_bytes(), text)
                threading.Thread(target=_summarize_doc, args=(doc["id"], text), daemon=True).start()
                trace.emit("router", "tool_result", {"saved_as": doc["kind"], "facts": list(doc["facts"])},
                           tool_name="docstore")
            if text.strip() and doc and doc["kind"] != "invoice":
                label = _KIND_LABEL.get(doc["kind"], "document")
                found = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in doc["facts"].items())
                reply = (f"Saved as: {label}." + (f" I found {found}." if found else "")
                         + " You can now ask me questions about it, and tick it in the Documents list if "
                           "you want it in your bank file.")
            elif text.strip():
                send_seed("bill", text, "en")
            if not text.strip():
                reply = ("I couldn't read any text from that photo. "
                         "Could you retake it with better light, closer and flat?")
            elif doc and doc["kind"] != "invoice":
                pass
            else:
                entry = LedgerAgent(conn, _ledger_llm).from_text(
                    bid, text, source="bill_photo", source_media_id="sim",
                    trace=trace)
                conn.commit()
                if entry.needs_review:
                    reply = (f"I've recorded a draft entry but need you to check it: "
                             f"{entry.review_reason}. Category: {entry.category}.")
                else:
                    reply = (f"Recorded: Rs {entry.amount_paise/100:,.2f} "
                             f"({entry.direction}, {entry.category})"
                             + (f", GST {entry.gst_rate_bp/100:.0f}%"
                                if entry.gst_rate_bp else "") + ". Added to your books.")
            trace.emit("router", "message_out", {"reply": reply[:300]})
            return {"reply": reply, "trace_id": str(trace.trace_id),
                    "ocr_text": text[:500]}
        finally:
            Path(tmp).unlink(missing_ok=True)
    finally:
        conn.close()


@app.post("/sim/voice")
async def sim_voice(
    file: UploadFile = File(...), language: str = Form("hi"),
    x_munim_id: Optional[str] = Header(None),
) -> dict:
    """Voice note: Bhashini ASR -> English reasoning -> reply translated back
    and spoken in the same language."""
    if _bhashini is None:
        return {"reply": "Bhashini isn't configured; voice messages aren't available."}
    conn = _get_conn()
    try:
        bid = _sim_business(conn, x_munim_id); conn.commit()
        trace = Trace(business_id=bid); trace.attach_db(conn)
        trace.emit("router", "message_in", {"type": "audio", "from": "sim", "language": language})
        audio_bytes = await file.read()
        trace.emit("router", "tool_call", {"bytes": len(audio_bytes)}, tool_name="bhashini.asr")
        try:
            text = _bhashini.speech_to_text(_to_wav(audio_bytes), language=language)  # type: ignore[arg-type]
        except Exception as exc:
            trace.emit("router", "error", {"stage": "asr", "error": str(exc)})
            return {"reply": f"Could not transcribe: {exc}", "trace_id": str(trace.trace_id)}
        trace.emit("router", "tool_result", {"transcript": text[:200]}, tool_name="bhashini.asr")
        out = _converse(text, language, bid, conn, trace, speak=True)
        out["transcript"] = text
        return out
    finally:
        conn.close()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _special(intent: str, text_en: str, bid: int, conn: psycopg.Connection, trace: Trace,
             lang: str, munim_id: Optional[str]):
    """Deliverables: the bank file, the application form prompt, answer export."""
    file_ev = None
    if intent == "dossier":
        yield _sse("stage", {"id": "dossier", "label": "Preparing your bank file"})
        pdf = DossierAgent(conn, _llm).build(bid, trace=trace)
        tok = _stash(pdf, "MunimAI-bank-file.pdf", "application/pdf")
        msg = ("Your bank file is ready. It has your books, GST summary, the proofs on file and what is still "
               "missing. Please review it before you share it. Tick the documents you want attached in the Documents list.")
        file_ev = {"url": f"/sim/file/{tok}", "name": "MunimAI-bank-file.pdf"}
    elif intent == "application":
        msg = ("I can draft the covering letter and loan application for your bank. Use the Loan application "
               "button to choose the bank, scheme, amount and purpose.")
        yield _sse("action", {"name": "open_application"})
    else:
        last = _LAST.get((munim_id or "")[:64])
        if not last:
            msg = "Ask me something first, then I can save the answer as a PDF or an image."
        else:
            png = any(w in text_en.lower() for w in ("image", "png", "picture", "jpeg"))
            fn = _export.reply_png if png else _export.reply_pdf
            data = fn(last["q"], last["reply"], last["reply_en"], last["lang"])
            name = "MunimAI-answer." + ("png" if png else "pdf")
            file_ev = {"url": f"/sim/file/{_stash(data, name, 'image/png' if png else 'application/pdf')}", "name": name}
            msg = "Here is your answer " + ("as an image." if png else "as a PDF.")
    try:
        text = localize_reply(_bhashini, msg, lang)
    except Exception:
        text = msg
    trace.emit("router", "message_out", {"reply": text[:300], "language": lang})
    yield _sse("reply", {"text": text, "text_en": msg, "language": lang})
    if file_ev:
        yield _sse("file", file_ev)


def _chat_stream(kind: str, payload, lang: str, speak: bool, munim_id: Optional[str]):
    """One live turn as a stream of events: each pipeline stage is announced the
    moment it starts, the reply arrives as soon as it is ready, and spoken audio
    follows clip by clip so playback begins before synthesis finishes."""
    conn = _get_conn()
    try:
        bid = _sim_business(conn, munim_id); conn.commit()
        trace = Trace(business_id=bid); trace.attach_db(conn)
        trace.emit("router", "message_in", {"type": kind, "from": "web", "language": lang})
        yield _sse("start", {"trace_id": str(trace.trace_id)})
        if kind == "audio":
            if _bhashini is None:
                yield _sse("fail", {"message": "Voice is not configured on the server."}); return
            yield _sse("stage", {"id": "hearing", "label": "Listening to your recording"})
            try:
                text = _bhashini.speech_to_text(_to_wav(payload), language=lang)  # type: ignore[arg-type]
            except Exception as exc:
                trace.emit("router", "error", {"stage": "asr", "error": str(exc)})
                yield _sse("fail", {"message": "I couldn't make out that recording. Please try again, closer to the mic."}); return
            trace.emit("router", "tool_result", {"transcript": text[:200]}, tool_name="bhashini.asr")
            if not text.strip():
                yield _sse("fail", {"message": "I didn't catch anything. Please try again."}); return
            yield _sse("heard", {"text": text})
        else:
            text = payload
        if lang == "auto":
            lang = detect_language(text)
        text_en = text
        if lang != "en":
            yield _sse("stage", {"id": "understanding", "label": "Translating to English"})
            try:
                text_en = to_english(_bhashini, text, lang)
            except Exception as exc:
                trace.emit("router", "error", {"stage": "translate_in", "error": str(exc)})
                text_en, lang = text, "en"
            trace.emit("router", "tool_result", {"language": lang, "understood": text_en[:200]}, tool_name="bhashini.translate")
        yield _sse("understood", {"text": text_en, "language": lang})
        send_seed("question", text_en, lang)
        intent = classify(text_en)
        if intent in ("dossier", "application", "export"):
            yield from _special(intent, text_en, bid, conn, trace, lang, munim_id)
            yield _sse("done", {}); return
        yield _sse("stage", {"id": "thinking", "label": "Checking the official rules"})
        reply_en = _handle_question(text_en, bid, conn, trace)
        reply = reply_en
        if lang != "en":
            yield _sse("stage", {"id": "translating", "label": "Writing the reply in your language"})
            try:
                reply = localize_reply(_bhashini, reply_en, lang)
            except Exception as exc:
                trace.emit("router", "error", {"stage": "translate_out", "error": str(exc)})
                reply, lang = reply_en, "en"
        trace.emit("router", "message_out", {"reply": reply[:300], "language": lang})
        _LAST[(munim_id or "")[:64]] = {"q": text, "reply": reply, "reply_en": reply_en, "lang": lang}
        yield _sse("reply", {"text": reply, "text_en": reply_en, "language": lang})
        if speak and lang in ("hi", "kn"):
            yield _sse("stage", {"id": "speaking", "label": "Preparing the voice"})
            try:
                for i, clip in enumerate(iter_clips(_bhashini, reply, lang)):
                    yield _sse("clip", {"i": i, "b64": clip})
            except Exception as exc:
                trace.emit("router", "error", {"stage": "tts", "error": str(exc)})
        yield _sse("done", {})
    except Exception as exc:  # never leave the browser waiting on a dead stream
        yield _sse("fail", {"message": "Something went wrong on my side. Please try again.", "detail": str(exc)[:200]})
    finally:
        conn.close()


@app.post("/sim/chat")
async def sim_chat(
    text: str = Form(""), language: str = Form("auto"), speak: str = Form("1"),
    file: Optional[UploadFile] = File(None), x_munim_id: Optional[str] = Header(None),
):
    audio = await file.read() if file is not None else None
    if audio:
        gen = _chat_stream("audio", audio, language, speak == "1", x_munim_id)
    else:
        if not text.strip():
            return {"error": "empty"}
        gen = _chat_stream("text", text.strip(), language, speak == "1", x_munim_id)
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/sim/documents")
def sim_documents(x_munim_id: Optional[str] = Header(None)) -> dict:
    conn = _get_conn()
    try:
        bid = _sim_business(conn, x_munim_id); conn.commit()
        return {"documents": docstore.list_docs(conn, bid),
                "banks": BANKS, "schemes": SCHEMES}
    finally:
        conn.close()


@app.post("/sim/documents/{doc_id}/include")
def sim_doc_include(doc_id: int, body: dict, x_munim_id: Optional[str] = Header(None)) -> dict:
    conn = _get_conn()
    try:
        bid = _sim_business(conn, x_munim_id)
        docstore.set_include(conn, bid, doc_id, bool(body.get("include")))
        return {"ok": True}
    finally:
        conn.close()


@app.post("/sim/dossier")
def sim_dossier(x_munim_id: Optional[str] = Header(None)) -> dict:
    conn = _get_conn()
    try:
        bid = _sim_business(conn, x_munim_id); conn.commit()
        trace = Trace(business_id=bid); trace.attach_db(conn)
        pdf = DossierAgent(conn, _llm).build(bid, trace=trace)
        return {"url": f"/sim/file/{_stash(pdf, 'MunimAI-bank-file.pdf', 'application/pdf')}",
                "name": "MunimAI-bank-file.pdf"}
    finally:
        conn.close()


@app.post("/sim/application")
def sim_application(body: dict, x_munim_id: Optional[str] = Header(None)) -> dict:
    try:
        req = AppRequest(
            bank=str(body.get("bank") or "Other bank")[:80], scheme=str(body.get("scheme") or "Other")[:80],
            amount_rupees=max(1000, int(float(body.get("amount") or 0))), purpose=str(body.get("purpose") or "").strip()[:400],
            tenure_months=max(6, min(120, int(body.get("tenure") or 36))),
            phone=str(body.get("phone") or "")[:20], address=str(body.get("address") or "")[:200])
    except (TypeError, ValueError):
        return {"error": "Enter the amount as a number."}
    if not req.purpose:
        return {"error": "Say what the loan is for."}
    lang = body.get("language", "en")
    tr = (lambda x: _bhashini.translate(x, "en", lang)) if (_bhashini and lang in ("hi", "kn")) else None
    conn = _get_conn()
    try:
        bid = _sim_business(conn, x_munim_id); conn.commit()
        trace = Trace(business_id=bid); trace.attach_db(conn)
        pdf = ApplicationAgent(conn, _llm, tr).build(bid, req, trace=trace)
        name = "MunimAI-loan-application.pdf"
        return {"url": f"/sim/file/{_stash(pdf, name, 'application/pdf')}", "name": name}
    finally:
        conn.close()


@app.post("/sim/export")
def sim_export(body: dict) -> dict:
    fmt = "png" if body.get("fmt") == "png" else "pdf"
    fn = _export.reply_png if fmt == "png" else _export.reply_pdf
    data = fn(str(body.get("question", ""))[:600], str(body.get("reply", ""))[:6000],
              str(body.get("reply_en", ""))[:6000], str(body.get("language", "en")))
    name = f"MunimAI-answer.{fmt}"
    return {"url": f"/sim/file/{_stash(data, name, 'image/png' if fmt == 'png' else 'application/pdf')}", "name": name}


@app.get("/sim/file/{token}")
def sim_file(token: str):
    item = _FILES.get(token)
    if not item:
        return JSONResponse({"error": "This file has expired. Create it again."}, status_code=404)
    data, name, mime = item
    return Response(content=data, media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/sim/trace/{trace_id}")
def sim_trace(trace_id: str) -> JSONResponse:
    """Return a trace as JSON so the UI can show the glass box."""
    from .trace import TRACE_DIR
    import json as _json
    path = TRACE_DIR / f"{trace_id}.jsonl"
    if not path.exists():
        return JSONResponse({"events": []})
    events = [_json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return JSONResponse({"events": events})


@app.get("/sim/")
def sim_root() -> FileResponse:
    """The simulator UI itself."""
    ui = Path(__file__).parent.parent / "sim_ui" / "index.html"
    return FileResponse(ui)


@app.get("/")
def sim_redirect() -> FileResponse:
    return sim_root()


@app.get("/health")
def health() -> dict:
    model_ok = _llm.health()
    try:
        _wa.verify_credentials()
        wa_ok = True
    except Exception:
        wa_ok = False
    return {"model": model_ok, "whatsapp": wa_ok, "bhashini": _bhashini is not None}
