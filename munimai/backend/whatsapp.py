"""
WhatsApp Cloud API client: send and parse messages.

Credentials are read from the environment only (see config.py). Media
(voice notes, bill photos) is downloaded through Meta's two-step media API:
first resolve the media id to a short-lived URL, then fetch the bytes —
both calls need the same bearer token.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

import requests

from .config import WhatsAppSettings

GRAPH_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"


class WhatsAppError(RuntimeError):
    pass


@dataclass
class InboundMessage:
    wa_message_id: str
    from_number: str          # the sender's WhatsApp number (E.164-ish, no +)
    msg_type: Literal["text", "audio", "image", "document", "unknown"]
    text: Optional[str] = None
    media_id: Optional[str] = None
    mime_type: Optional[str] = None


class WhatsAppClient:
    def __init__(self, settings: Optional[WhatsAppSettings] = None) -> None:
        self.s = settings or WhatsAppSettings.from_env()
        self._headers = {"Authorization": f"Bearer {self.s.token}"}

    # ------------------------------------------------------------- outbound
    def send_text(self, to: str, body: str) -> str:
        """Send a plain text reply. Returns the outbound wamid."""
        payload = {
            "messaging_product": "whatsapp", "to": to, "type": "text",
            "text": {"body": body[:4096]},   # WhatsApp's own text cap
        }
        data = self._post(f"{self.s.phone_number_id}/messages", payload)
        return data["messages"][0]["id"]

    def send_audio_by_id(self, to: str, media_id: str) -> str:
        payload = {"messaging_product": "whatsapp", "to": to, "type": "audio",
                   "audio": {"id": media_id}}
        data = self._post(f"{self.s.phone_number_id}/messages", payload)
        return data["messages"][0]["id"]

    def upload_media(self, file_path: str, mime_type: str) -> str:
        """Upload local bytes (e.g. a Bhashini TTS wav) and get a media id back,
        for use with send_audio_by_id."""
        url = f"{GRAPH_BASE}/{self.s.phone_number_id}/media"
        with open(file_path, "rb") as fh:
            files = {"file": (Path(file_path).name, fh, mime_type)}
            data = {"messaging_product": "whatsapp", "type": mime_type}
            r = requests.post(url, headers=self._headers, data=data,
                              files=files, timeout=60)
        if not r.ok:
            raise WhatsAppError(f"media upload failed: {r.status_code} {r.text[:300]}")
        return r.json()["id"]

    # -------------------------------------------------------------- inbound
    @staticmethod
    def parse_webhook(body: dict[str, Any]) -> list[InboundMessage]:
        """Pull InboundMessage objects out of a raw webhook POST body.
        Meta batches changes; a single POST can carry zero or more messages
        (e.g. a status update has none)."""
        out: list[InboundMessage] = []
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for m in value.get("messages", []):
                    out.append(WhatsAppClient._parse_one(m))
        return out

    @staticmethod
    def _parse_one(m: dict[str, Any]) -> InboundMessage:
        wa_id = m["id"]
        frm = m["from"]
        mtype = m.get("type", "unknown")
        if mtype == "text":
            return InboundMessage(wa_id, frm, "text", text=m["text"]["body"])
        if mtype == "audio":
            a = m["audio"]
            return InboundMessage(wa_id, frm, "audio", media_id=a["id"],
                                  mime_type=a.get("mime_type"))
        if mtype == "image":
            i = m["image"]
            return InboundMessage(wa_id, frm, "image", media_id=i["id"],
                                  mime_type=i.get("mime_type"),
                                  text=i.get("caption"))
        return InboundMessage(wa_id, frm, "unknown")

    def download_media(self, media_id: str, dest_path: str) -> str:
        """Two-step: resolve media id -> short-lived URL -> fetch bytes.
        Returns the mime type reported by the first call."""
        meta = self._get(media_id)
        url = meta["url"]
        mime_type = meta.get("mime_type", "application/octet-stream")
        r = requests.get(url, headers=self._headers, timeout=60)
        if not r.ok:
            raise WhatsAppError(f"media download failed: {r.status_code}")
        Path(dest_path).write_bytes(r.content)
        return mime_type

    # ------------------------------------------------------------ internals
    def _post(self, path: str, payload: dict) -> dict:
        r = requests.post(f"{GRAPH_BASE}/{path}", headers=self._headers,
                          json=payload, timeout=30)
        if not r.ok:
            raise WhatsAppError(f"POST {path} -> {r.status_code}: {r.text[:400]}")
        return r.json()

    def _get(self, path: str, **params) -> dict:
        r = requests.get(f"{GRAPH_BASE}/{path}", headers=self._headers,
                         params=params, timeout=30)
        if not r.ok:
            raise WhatsAppError(f"GET {path} -> {r.status_code}: {r.text[:400]}")
        return r.json()

    def verify_credentials(self) -> dict:
        """Confirm the token/number are live. Raises WhatsAppError if not."""
        return self._get(self.s.phone_number_id,
                         fields="verified_name,display_phone_number,quality_rating")
