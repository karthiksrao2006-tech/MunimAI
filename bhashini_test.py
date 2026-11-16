"""
bhashini_test.py
----------------
Round-trip smoke test for the Bhashini client: text -> speech -> text, in both
Hindi and Kannada.

Run:  python bhashini_test.py

Exits cleanly with an explanation if credentials are not set. Results with the
original Devanagari/Kannada text are written to bhashini_test_result.json in
UTF-8, because the Windows console (cp1252) cannot print those scripts.
"""

from __future__ import annotations

import json
import sys

from dotenv import load_dotenv

from bhashini_client import (
    BhashiniAPIError,
    BhashiniClient,
    BhashiniConfigError,
)

load_dotenv()

CASES: list[tuple[str, str]] = [
    ("hi", "नमस्ते, मैं मुनीम ए आई हूँ। आपका जीएसटी रिटर्न तैयार है।"),
    ("kn", "ನಮಸ್ಕಾರ, ನಾನು ಮುನೀಮ್ ಎ ಐ. ನಿಮ್ಮ ಜಿಎಸ್‌ಟಿ ರಿಟರ್ನ್ ಸಿದ್ಧವಾಗಿದೆ."),
]

SCRIPT_RANGES = {
    "hi": (0x0900, 0x097F),  # Devanagari
    "kn": (0x0C80, 0x0CFF),  # Kannada
}


def script_ratio(text: str, language: str) -> float:
    """Fraction of letters that are in the expected script. Catches the common
    failure where a model or ASR returns Romanised text instead."""
    lo, hi = SCRIPT_RANGES[language]
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if lo <= ord(c) <= hi) / len(letters)


def main() -> int:
    try:
        client = BhashiniClient()
    except BhashiniConfigError as exc:
        print("Bhashini credentials not configured.\n")
        print(exc)
        print("\nCopy .env.example to .env, fill in both keys, then re-run.")
        return 0  # a missing key is a setup state, not a crash

    results: dict[str, object] = {}
    failures = 0

    for language, phrase in CASES:
        print(f"--- {language} ---")
        try:
            tts_service = client.get_service_id("tts", language)
            asr_service = client.get_service_id("asr", language)
            print(f"  tts service : {tts_service}")
            print(f"  asr service : {asr_service}")

            audio = client.text_to_speech(phrase, language=language)
            out_wav = f"bhashini_{language}.wav"
            with open(out_wav, "wb") as fh:
                fh.write(audio)
            print(f"  TTS         : OK ({len(audio)} bytes -> {out_wav})")

            transcript = client.speech_to_text(audio, language=language)
            ratio = script_ratio(transcript, language)
            print(f"  ASR         : OK ({len(transcript)} chars, "
                  f"{ratio:.0%} in expected script)")

            results[language] = {
                "spoken": phrase,
                "transcribed": transcript,
                "script_ratio": round(ratio, 3),
                "tts_service": tts_service,
                "asr_service": asr_service,
                "audio_bytes": len(audio),
            }
            if ratio < 0.8:
                print("  WARNING     : transcript is mostly outside the expected script")
                failures += 1

        except (BhashiniAPIError, BhashiniConfigError) as exc:
            print(f"  FAILED      : {exc}")
            results[language] = {"error": str(exc)}
            failures += 1

    with open("bhashini_test_result.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)

    print("\nFull results (with original scripts) -> bhashini_test_result.json")
    if failures:
        print(f"{failures} case(s) had problems.")
        return 1
    print("All cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
