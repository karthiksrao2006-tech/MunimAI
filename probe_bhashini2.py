"""
probe_bhashini2.py
------------------
Second probe: the COMPUTE call.

Probe 1 showed the config endpoint returns only {languages, pipelineResponseConfig}
-- no callbackUrl / inferenceApiKey block. So the compute endpoint is not handed
to us; we must call Dhruva directly with the Inference key.

This script:
  1. Resolves serviceIds for ASR and TTS, in Hindi and Kannada.
  2. Attempts a real TTS call (text -> audio) against candidate Dhruva endpoints
     and auth header formats, reporting exactly what each returns.
  3. If TTS succeeds, saves the audio and round-trips it back through ASR.
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
PIPELINE_ID_MEITY = "64392f96daac500b55c543cd"
CONFIG_URL = "https://meity-auth.ulcacontrib.org/ulca/apis/v0/model/getModelsPipeline"

COMPUTE_ENDPOINTS = [
    "https://dhruva-api.bhashini.gov.in/services/inference/pipeline",
]

AUTH_VARIANTS = [
    ("Authorization: <inference>", {"Authorization": INFERENCE}),
    ("Authorization: Bearer <inference>", {"Authorization": f"Bearer {INFERENCE}"}),
    ("x-auth-source + Authorization", {"Authorization": INFERENCE, "x-auth-source": "API_KEY"}),
    ("Authorization: <udyat>", {"Authorization": UDYAT}),
]


def snip(t: str, n: int = 400) -> str:
    t = " ".join(t.split())
    return t[:n] + ("..." if len(t) > n else "")


def get_service_id(task: str, lang: str) -> Optional[str]:
    body: dict[str, Any] = {
        "pipelineTasks": [
            {"taskType": task, "config": {"language": {"sourceLanguage": lang}}}
        ],
        "pipelineRequestConfig": {"pipelineId": PIPELINE_ID_MEITY},
    }
    r = requests.post(
        CONFIG_URL, json=body, headers={"ulcaApiKey": UDYAT}, timeout=25
    )
    if not r.ok:
        print(f"  config {task}/{lang}: HTTP {r.status_code} {snip(r.text,150)}")
        return None
    data = r.json()
    for cfg in data.get("pipelineResponseConfig", []):
        if cfg.get("taskType") == task:
            try:
                return cfg["config"][0]["serviceId"]
            except (KeyError, IndexError):
                return None
    return None


def main() -> None:
    print("=" * 72)
    print("STEP 1 — resolve serviceIds")
    print("=" * 72)
    services: dict[str, Optional[str]] = {}
    for task in ("asr", "tts"):
        for lang in ("hi", "kn"):
            sid = get_service_id(task, lang)
            services[f"{task}-{lang}"] = sid
            print(f"  {task:4} {lang}: {sid}")

    tts_hi = services.get("tts-hi")
    if not tts_hi:
        print("\nNo Hindi TTS serviceId resolved; cannot probe compute call.")
        return

    print()
    print("=" * 72)
    print("STEP 2 — live TTS compute call")
    print("=" * 72)
    payload = {
        "pipelineTasks": [
            {
                "taskType": "tts",
                "config": {
                    "language": {"sourceLanguage": "hi"},
                    "serviceId": tts_hi,
                    "gender": "female",
                    "samplingRate": 8000,
                },
            }
        ],
        "inputData": {
            "input": [{"source": "नमस्ते, मैं मुनीम ए आई हूँ।"}],
        },
    }

    working = None
    for url in COMPUTE_ENDPOINTS:
        for label, hdrs in AUTH_VARIANTS:
            headers = {"Content-Type": "application/json", **hdrs}
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=60)
            except requests.RequestException as exc:
                print(f"  [{label:34}] NETWORK ERROR {exc}")
                continue
            print(f"  [{label:34}] HTTP {r.status_code}")
            if r.ok:
                try:
                    data = r.json()
                except ValueError:
                    print("      non-JSON:", snip(r.text, 200))
                    continue
                print("      top-level keys:", list(data.keys()))
                print("      shape:", snip(json.dumps(data), 500))
                working = (url, headers, data)
                break
            else:
                print("      body:", snip(r.text, 250))
        if working:
            break

    if not working:
        print("\nNo compute auth variant worked. See bodies above.")
        return

    url, headers, data = working
    print()
    print("=" * 72)
    print("STEP 3 — parse + save TTS audio")
    print("=" * 72)
    audio_b64 = None
    try:
        audio_b64 = data["pipelineResponse"][0]["audio"][0]["audioContent"]
        print("  parsed via pipelineResponse[0].audio[0].audioContent  <-- convention HOLDS")
    except (KeyError, IndexError, TypeError) as exc:
        print(f"  convention FAILED ({exc}); dumping structure:")
        print("  ", snip(json.dumps(data), 1500))

    if audio_b64:
        raw = base64.b64decode(audio_b64)
        out = "bhashini_tts_sample.wav"
        with open(out, "wb") as fh:
            fh.write(raw)
        print(f"  saved {len(raw)} bytes -> {out}")

        asr_hi = services.get("asr-hi")
        if asr_hi:
            print()
            print("=" * 72)
            print("STEP 4 — round-trip that audio back through ASR")
            print("=" * 72)
            asr_payload = {
                "pipelineTasks": [
                    {
                        "taskType": "asr",
                        "config": {
                            "language": {"sourceLanguage": "hi"},
                            "serviceId": asr_hi,
                            "preProcessors": [],
                            "postProcessors": [],
                        },
                    }
                ],
                "inputData": {"audio": [{"audioContent": audio_b64}]},
            }
            r = requests.post(url, json=asr_payload, headers=headers, timeout=60)
            print(f"  ASR HTTP {r.status_code}")
            if r.ok:
                d = r.json()
                print("  shape:", snip(json.dumps(d, ensure_ascii=False), 600))
                try:
                    text = d["pipelineResponse"][0]["output"][0]["source"]
                    print(f"  TRANSCRIBED: {text}")
                except (KeyError, IndexError, TypeError) as exc:
                    print(f"  ASR convention FAILED ({exc})")
            else:
                print("  body:", snip(r.text, 300))


if __name__ == "__main__":
    main()
