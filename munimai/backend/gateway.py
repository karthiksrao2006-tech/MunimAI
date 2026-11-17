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

import tempfile
from pathlib import Path
from typing import Optional

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

from .agents.ledger import LedgerAgent
from .agents.scheme import SchemeAgent
from .agents.verifier import Verifier
from .router import CANNED, classify
from .bhashini_client import BhashiniClient, BhashiniConfigError
from .config import DBSettings, LLMSettings, WhatsAppSettings
from .llm import LLM
from .ocr import RemoteEasyOCR
from .trace import Trace
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


def _get_conn() -> psycopg.Connection:
    return psycopg.connect(_db.conninfo)


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
        text = _bhashini.speech_to_text(audio_bytes, language="hi")
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


def _handle_question(question: str, business_id: int,
                     conn: psycopg.Connection, trace: Trace) -> str:
    intent = classify(question)
    trace.emit("router", "route", {"intent": intent})
    if intent in CANNED:
        return CANNED[intent]
    # eligibility, or unknown fallback -> scheme
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


def _sim_business(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM businesses WHERE whatsapp_phone=%s", (_SIM_PHONE,)
    ).fetchone()
    if row:
        return row[0]
    return conn.execute(
        "INSERT INTO businesses (whatsapp_phone, display_name, language, is_synthetic)"
        " VALUES (%s,%s,'kn',TRUE) RETURNING id",
        (_SIM_PHONE, "Lakshmi Tailoring Unit"),
    ).fetchone()[0]


@app.post("/sim/text")
async def sim_text(body: dict) -> dict:
    """Simulated text message in, agent reply out."""
    text = (body.get("text") or "").strip()
    if not text:
        return {"reply": "(empty message)"}
    conn = _get_conn()
    try:
        bid = _sim_business(conn); conn.commit()
        trace = Trace(business_id=bid); trace.attach_db(conn)
        trace.emit("router", "message_in", {"type": "text", "from": "sim"})
        reply = _handle_question(text, bid, conn, trace)
        trace.emit("router", "message_out", {"reply": reply[:300]})
        return {"reply": reply, "trace_id": str(trace.trace_id)}
    finally:
        conn.close()


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
async def sim_image(file: UploadFile = File(...)) -> dict:
    """Simulated bill upload (photo, PNG/JPEG or PDF). Runs the real Ledger
    pipeline (OCR -> structure)."""
    conn = _get_conn()
    try:
        bid = _sim_business(conn); conn.commit()
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
            if not text.strip():
                reply = ("I couldn't read any text from that photo. "
                         "Could you retake it with better light, closer and flat?")
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
) -> dict:
    """Simulated voice note. Bhashini ASR -> same text path as /sim/text."""
    if _bhashini is None:
        return {"reply": "Bhashini isn't configured; voice messages aren't available."}
    conn = _get_conn()
    try:
        bid = _sim_business(conn); conn.commit()
        trace = Trace(business_id=bid); trace.attach_db(conn)
        trace.emit("router", "message_in", {"type": "audio", "from": "sim"})
        audio_bytes = await file.read()
        trace.emit("router", "tool_call", {"bytes": len(audio_bytes)},
                   tool_name="bhashini.asr")
        try:
            text = _bhashini.speech_to_text(audio_bytes, language=language)  # type: ignore[arg-type]
        except Exception as exc:
            trace.emit("router", "error", {"stage": "asr", "error": str(exc)})
            return {"reply": f"Could not transcribe: {exc}",
                    "trace_id": str(trace.trace_id)}
        trace.emit("router", "tool_result",
                   {"transcript": text[:200]}, tool_name="bhashini.asr")
        reply = _handle_question(text, bid, conn, trace)
        trace.emit("router", "message_out", {"reply": reply[:300]})
        return {"reply": reply, "transcript": text,
                "trace_id": str(trace.trace_id)}
    finally:
        conn.close()


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
