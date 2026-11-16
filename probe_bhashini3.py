"""
probe_bhashini3.py
------------------
Final verification: full TTS -> ASR round trip, with UTF-8-safe output.
Windows console is cp1252 and cannot print Devanagari, so results are written
to bhashini_probe_result.json (UTF-8) and only ASCII status lines are printed.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

UDYAT = os.environ.get("BHASHINI_UDYAT_KEY", "").strip()
INFERENCE = os.environ.get("BHASHINI_INFERENCE_KEY", "").strip()
PIPELINE_ID = "64392f96daac500b55c543cd"
CONFIG_URL = "https://meity-auth.ulcacontrib.org/ulca/apis/v0/model/getModelsPipeline"
COMPUTE_URL = "https://dhruva-api.bhashini.gov.in/services/inference/pipeline"
AUTH = {"Content-Type": "application/json", "Authorization": INFERENCE}

results: dict[str, Any] = {}


def service_id(task: str, lang: str) -> Optional[str]:
    body = {
        "pipelineTasks": [{"taskType": task, "config": {"language": {"sourceLanguage": lang}}}],
        "pipelineRequestConfig": {"pipelineId": PIPELINE_ID},
    }
    r = requests.post(CONFIG_URL, json=body, headers={"ulcaApiKey": UDYAT}, timeout=25)
    r.raise_for_status()
    for cfg in r.json().get("pipelineResponseConfig", []):
        if cfg.get("taskType") == task:
            return cfg["config"][0]["serviceId"]
    return None


def tts(text: str, lang: str, sid: str) -> Optional[str]:
    payload = {
        "pipelineTasks": [{
            "taskType": "tts",
            "config": {
                "language": {"sourceLanguage": lang},
                "serviceId": sid,
                "gender": "female",
                "samplingRate": 8000,
            },
        }],
        "inputData": {"input": [{"source": text}]},
    }
    r = requests.post(COMPUTE_URL, json=payload, headers=AUTH, timeout=60)
    if not r.ok:
        return None
    return r.json()["pipelineResponse"][0]["audio"][0]["audioContent"]


def asr(audio_b64: str, lang: str, sid: str) -> Optional[str]:
    payload = {
        "pipelineTasks": [{
            "taskType": "asr",
            "config": {
                "language": {"sourceLanguage": lang},
                "serviceId": sid,
                "preProcessors": [],
                "postProcessors": [],
            },
        }],
        "inputData": {"audio": [{"audioContent": audio_b64}]},
    }
    r = requests.post(COMPUTE_URL, json=payload, headers=AUTH, timeout=60)
    if not r.ok:
        results.setdefault("errors", []).append({"stage": "asr", "status": r.status_code, "body": r.text[:400]})
        return None
    data = r.json()
    results.setdefault("raw_asr_shape", {})[lang] = list(data.keys())
    return data["pipelineResponse"][0]["output"][0]["source"]


def run(lang: str, phrase: str) -> None:
    print(f"--- {lang} ---")
    sid_tts = service_id("tts", lang)
    sid_asr = service_id("asr", lang)
    print(f"  tts service: {sid_tts}")
    print(f"  asr service: {sid_asr}")
    results.setdefault("services", {})[lang] = {"tts": sid_tts, "asr": sid_asr}

    audio = tts(phrase, lang, sid_tts) if sid_tts else None
    if not audio:
        print("  TTS: FAILED")
        return
    raw = base64.b64decode(audio)
    fname = f"bhashini_{lang}.wav"
    with open(fname, "wb") as fh:
        fh.write(raw)
    print(f"  TTS: OK ({len(raw)} bytes -> {fname})")

    text = asr(audio, lang, sid_asr) if sid_asr else None
    if text is None:
        print("  ASR: FAILED (see json)")
        return
    print(f"  ASR: OK ({len(text)} chars transcribed)")
    results.setdefault("roundtrip", {})[lang] = {"spoken": phrase, "transcribed": text}


if __name__ == "__main__":
    run("hi", "नमस्ते, मैं मुनीम ए आई हूँ। आपका जीएसटी रिटर्न तैयार है।")
    run("kn", "ನಮಸ್ಕಾರ, ನಾನು ಮುನೀಮ್ ಎ ಐ. ನಿಮ್ಮ ಜಿಎಸ್‌ಟಿ ರಿಟರ್ನ್ ಸಿದ್ಧವಾಗಿದೆ.")

    with open("bhashini_probe_result.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    print("\nFull results (with Devanagari/Kannada text) -> bhashini_probe_result.json")
