"""
Language handling for the voice assistant: whatever language the owner uses
(Kannada, Hindi, English), reasoning happens in English and the reply is
translated back and, for Hindi/Kannada, spoken with Bhashini TTS.

The "(Sources: ...)" citation tail is never translated or spoken: clause ids
and official URLs must stay exactly as stored.
"""

from __future__ import annotations

import base64
import re
from typing import Optional

from .bhashini_client import BhashiniClient

_KN = re.compile(r"[ಀ-೿]")
_HI = re.compile(r"[ऀ-ॿ]")
_MAX_SPOKEN_CHARS = 700
_CLIP_CHARS = 120


def detect_language(text: str) -> str:
    """Script-based: Kannada or Devanagari characters win, otherwise English."""
    kn, hi = len(_KN.findall(text)), len(_HI.findall(text))
    if kn == 0 and hi == 0:
        return "en"
    return "kn" if kn >= hi else "hi"


def split_sources(reply: str) -> tuple[str, str]:
    i = reply.find("(Sources:")
    return (reply[:i].strip(), reply[i:].strip()) if i >= 0 else (reply.strip(), "")


def to_english(bh: Optional[BhashiniClient], text: str, lang: str) -> str:
    if lang == "en" or bh is None:
        return text
    return bh.translate(text, lang, "en")


def localize_reply(bh: Optional[BhashiniClient], reply_en: str, lang: str) -> str:
    if lang == "en" or bh is None:
        return reply_en
    body, tail = split_sources(reply_en)
    local = bh.translate(body, "en", lang) if body else ""
    return f"{local}\n\n{tail}" if tail else local


def _pieces(sentence: str) -> list[str]:
    """Break a long sentence at commas, then spaces, so every clip stays short."""
    if len(sentence) <= _CLIP_CHARS:
        return [sentence]
    out, cur = [], ""
    for part in re.split(r"(?<=,)\s+|\s+", sentence):
        if cur and len(cur) + len(part) + 1 > _CLIP_CHARS:
            out.append(cur)
            cur = part
        else:
            cur = f"{cur} {part}".strip()
    if cur:
        out.append(cur)
    return out


def _chunks(text: str) -> list[str]:
    sentences = [p for s in re.split(r"(?<=[.!?।])\s+", text.strip()) for p in _pieces(s)]
    out, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) > _CLIP_CHARS:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def iter_clips(bh: Optional[BhashiniClient], reply_local: str, lang: str):
    """Yield base64 WAV clips one sentence-group at a time, so playback can start
    while later clips are still being synthesised. Citations are never spoken;
    Hindi/Kannada only (English is spoken by the browser)."""
    if bh is None or lang not in ("hi", "kn"):
        return
    body, _ = split_sources(reply_local)
    used = 0
    for chunk in _chunks(body):
        if used + len(chunk) > _MAX_SPOKEN_CHARS:
            break
        yield base64.b64encode(bh.text_to_speech(chunk, lang, sampling_rate=16000)).decode("ascii")
        used += len(chunk)


def speak_clips(bh: Optional[BhashiniClient], reply_local: str, lang: str) -> list[str]:
    return list(iter_clips(bh, reply_local, lang))
