"""
Tiered Cognition Router — a small classifier that decides what a message
actually wants, so the pipeline does not force every message through the
scheme-eligibility path.

Deliberately NOT an LLM call for intent:
- Zero-latency, deterministic, free.
- Qwen-7B scored 0.90 on JSON tool calls, not 1.00. A wrong intent means a
  wrong agent. For a handful of intent classes, a keyword matcher is both
  cheaper and more accurate than a 7B model.
- The model still does the hard work (answering) once we know where to send.

Intents returned:
  'eligibility' - "can I get a loan", "am I eligible", "mudra kishore?"
  'documents'   - "what documents", "what to send", "kya dastavez"
  'capability'  - "can you speak kannada", "what can you do", "how do you work"
  'greeting'    - "hi", "hello", "namaste"
  'thanks'      - "thanks", "dhanyavad"
  'unknown'     - fallback (routes to Scheme as before)
"""

from __future__ import annotations

import re

Intent = str

# Short canned replies for the intents that don't need the LLM at all.
# Grounded in what MunimAI really does, not model imagination.
CANNED = {
    "greeting": (
        "Namaste! I'm MunimAI — your WhatsApp munim.\n\n"
        "Send me a bill photo, ask about a MUDRA loan, or record a voice "
        "note in Hindi or Kannada. I'll do the rest."
    ),
    "thanks": "Anytime. Send another bill or ask another question whenever you need.",
    "capability": (
        "Here is what I can do right now:\n\n"
        "- Read bill photos and add the entry to your books (amount, GST rate, counterparty).\n"
        "- Answer MUDRA loan eligibility questions — every answer cites the official clause.\n"
        "- Prepare a GST summary (output tax, input credit, net payable) from your books.\n"
        "- Build an affordability view a banker can read.\n\n"
        "I understand Hindi and Kannada voice notes (through Bhashini). I never file "
        "a return or submit a loan on your behalf — I prepare; you approve."
    ),
    "documents": (
        "For MunimAI to work, send me what the business already has:\n\n"
        "- A clear photo of each supplier and customer bill (I'll extract the amount and GST).\n"
        "- Your Udyam number and GSTIN (if you have them — I can work without).\n"
        "- A WhatsApp voice note describing anything that isn't on a bill.\n\n"
        "For a MUDRA loan application itself, the bank decides what it needs. "
        "MUDRA loans up to Rs 20 lakh are collateral-free and PAN is not compulsory "
        "(MUDRA FAQ). I'll ask about the specific bank's checklist once you tell me which one."
    ),
}


def classify(text: str) -> Intent:
    t = (text or "").strip().lower()
    if not t:
        return "unknown"

    # eligibility — strong signals win first
    if _any(t, [
        r"\bmudra\b", r"\bkishore\b", r"\bshishu\b", r"\btarun\b",
        r"\bloan\b", r"\bgive me money\b", r"\beligib", r"\bqualify\b",
        r"\bam i (eligible|able)\b", r"\bpaisa\b", r"\brupees\b",
        r"\bfinance\b", r"\bbusiness loan\b",
    ]):
        return "eligibility"

    if _any(t, [
        r"\bwhat (do|should|all) (i|we) (give|send|share|provide)\b",
        r"\bwhat document", r"\bdocuments?\b", r"\bdastavez", r"\bkagaz\b",
        r"\bwhat to send\b", r"\bkyc\b", r"\bpaperwork\b",
        r"\bwhat (will|do) (you|u) need\b",
    ]):
        return "documents"

    if _any(t, [
        r"\bcan (you|u) speak\b", r"\bdo you (speak|know|support)\b",
        r"\bwhat can you do\b", r"\bhow do you work\b",
        r"\bwho are you\b", r"\bwhat are you\b", r"\bare you an? ai\b",
        r"\blanguage(s)?\b", r"\bkannada\b", r"\bhindi\b", r"\btelugu\b",
        r"\btamil\b",
    ]):
        return "capability"

    if _any(t, [
        r"^\s*(hi|hello|hey|namaste|namaskara|namaskar|vanakkam|salaam)[\s!,.]*$",
        r"^\s*good (morning|afternoon|evening|night)\b",
    ]):
        return "greeting"

    if _any(t, [
        r"^\s*(thanks|thank you|thnx|dhanyavad|shukriya)[\s!,.]*$",
    ]):
        return "thanks"

    return "unknown"


def _any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text) for p in patterns)
