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
# Grounded in what SaralcreditAI really does, not model imagination.
CANNED = {
    "greeting": (
        "Namaste! I'm SaralcreditAI, your credit and compliance helper.\n\n"
        "Send me a bill photo, ask about a MUDRA loan, or record a voice "
        "note in Hindi or Kannada. I'll do the rest."
    ),
    "thanks": "Anytime. Send another bill or ask another question whenever you need.",
    "capability": (
        "Here is what I can do:\n\n"
        "- Answer loan, subsidy and startup-funding questions from official scheme pages "
        "(MUDRA, PMEGP, CGTMSE, SVANidhi, Stand-Up India, Startup India and more), always naming the clause.\n"
        "- Read your bills, PDFs and certificates, add bills to your books, and answer questions about your documents.\n"
        "- Search the web when the official pages do not cover a question, and say clearly where the answer came from.\n"
        "- Prepare a GST summary and a bank file (PDF), and draft a loan application and covering letter.\n"
        "- Save any answer as a PDF or an image.\n\n"
        "I understand and reply in Kannada, Hindi and English, by voice or text. I never file a return or "
        "submit a loan for you: I prepare, you review and decide."
    ),
    "documents": (
        "For SaralcreditAI to work, send me what the business already has:\n\n"
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

    # deliverables first: they contain words ("loan", "documents") that would
    # otherwise route to eligibility or the canned documents reply
    if _any(t, [r"\bbank[- ]?(ready )?(file|dossier|pack)\b", r"\bloan file\b", r"\bdossier\b",
                r"\bprepare (my|the) (file|documents|pack)\b", r"\bpdf (of|with) my (documents|books|proofs)\b"]):
        return "dossier"
    if _any(t, [r"\b(draft|write|prepare|make|create)\b.*\b(loan )?application\b", r"\bapplication (letter|form|draft)\b",
                r"\bletter to (the )?(bank|sbi|branch)\b", r"\bdetailed draft\b"]):
        return "application"
    if _any(t, [r"\b(as|in|into|to) (a |an )?(pdf|image|png|jpeg|picture)\b",
                r"\b(save|send|give|download|make|export)\b.*\b(pdf|image|png|picture)\b"]):
        return "export"

    # eligibility — strong signals win first
    if _any(t, [
        r"\bmudra\b", r"\bkishore\b", r"\bshishu\b", r"\btarun\b",
        r"\bloan\b", r"\bgive me money\b", r"\beligib", r"\bqualify\b",
        r"\bam i (eligible|able)\b", r"\bpaisa\b", r"\brupees\b",
        r"\bfinance\b", r"\bbusiness loan\b",
        r"\bpmegp\b", r"\bcgtmse\b", r"\bsvanidhi\b", r"\bstand[- ]?up\b", r"\bstartup\b", r"\bsubsid",
        r"\bseed fund\b", r"\bfund of funds\b", r"\bnidhi\b", r"\bvishwakarma\b", r"\bnsic\b",
        r"\bgrant\b", r"\bsidbi\b", r"\bjan samarth\b", r"\bcollateral\b", r"\bguarantee\b",
        r"\b(which|what) (government |govt )?(scheme|schemes|subsidy|loan)\b", r"\bseed (funding|capital)\b",
        r"\b(government|govt|central|state) (loan |msme )?schemes?\b", r"\bmsme schemes?\b",
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
