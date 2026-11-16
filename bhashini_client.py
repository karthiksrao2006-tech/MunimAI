"""
bhashini_client.py
------------------
Client for Bhashini (MeitY / National Language Translation Mission) used by
MunimAI for voice-first interaction in Hindi and Kannada.

Where this sits in the architecture: a WhatsApp voice note is converted to text
BEFORE anything reaches the LLM. speech_to_text() produces the text that the
router and orchestrator then act on. text_to_speech() optionally turns a reply
back into audio for a voice-note response.

API SHAPE — VERIFIED EMPIRICALLY against live credentials on 2026-11-16.
An earlier draft of this file assumed the older ULCA scheme and was wrong in
three ways; all three are corrected here and the corrections are confirmed by
a working Hindi and Kannada round trip (see probe_bhashini3.py).

  1. CONFIG CALL — discovers the serviceId for a task + language.
       POST https://meity-auth.ulcacontrib.org/ulca/apis/v0/model/getModelsPipeline
       Headers: {"ulcaApiKey": <UDYAT KEY>}
       CORRECTION: do NOT send a `userID` header. Sending one makes the call
       fail with HTTP 400 "Error in fetching ulcaApiKey".
       CORRECTION: the response contains ONLY {"languages", "pipelineResponseConfig"}.
       It does NOT return `pipelineInferenceAPIEndPoint`, `callbackUrl` or
       `inferenceApiKey`, so the compute endpoint is NOT discovered here and
       must be addressed directly (see 2).
       serviceId path: pipelineResponseConfig[i].config[0].serviceId
                       where pipelineResponseConfig[i].taskType == task

  2. COMPUTE CALL — performs the actual ASR or TTS.
       POST https://dhruva-api.bhashini.gov.in/services/inference/pipeline
       Headers: {"Authorization": <INFERENCE KEY>}
       CORRECTION: this URL is fixed, not returned by the config call, and the
       auth header is the Inference key, not the Udyat key.

  3. RESPONSE PARSING — both confirmed against real responses:
       TTS: pipelineResponse[0]["audio"][0]["audioContent"]  (base64 wav)
       ASR: pipelineResponse[0]["output"][0]["source"]       (text)

Verified services (resolved live, may change — they are looked up, not hardcoded):
    asr hi -> ai4bharat/conformer-hi-gpu--t4
    asr kn -> ai4bharat/conformer-multilingual-dravidian-gpu--t4
    tts hi -> ai4bharat/indic-tts-coqui-indo_aryan-gpu--t4
    tts kn -> ai4bharat/indic-tts-coqui-dravidian-gpu--t4

Credentials come from the environment only, never hardcoded:
    BHASHINI_UDYAT_KEY      - the Udyat key, used on the config call
    BHASHINI_INFERENCE_KEY  - the Inference key, used on the compute call
Both are shown under "My Profile" at https://bhashini.gov.in.
"""

from __future__ import annotations

import base64
import os
import threading
from dataclasses import dataclass
from typing import Literal, Optional

import requests

LanguageCode = Literal["hi", "kn"]
TaskType = Literal["asr", "tts"]

_SUPPORTED_LANGUAGES: dict[str, str] = {"hi": "Hindi", "kn": "Kannada"}

CONFIG_URL = "https://meity-auth.ulcacontrib.org/ulca/apis/v0/model/getModelsPipeline"
COMPUTE_URL = "https://dhruva-api.bhashini.gov.in/services/inference/pipeline"

# MeitY pipeline. The AI4Bharat pipeline (643930aa521a4b1ba0f4c41d) has no ASR.
PIPELINE_ID_MEITY = "64392f96daac500b55c543cd"

_CONFIG_TIMEOUT = 30
_COMPUTE_TIMEOUT = 90  # audio generation is slower than a config lookup


class BhashiniConfigError(RuntimeError):
    """Credentials missing, or rejected by Bhashini."""


class BhashiniUnsupportedLanguageError(ValueError):
    """A language outside _SUPPORTED_LANGUAGES was requested."""


class BhashiniAPIError(RuntimeError):
    """Bhashini returned an error or an unexpected response shape."""


@dataclass(frozen=True)
class _CacheKey:
    task: str
    language: str


class BhashiniClient:
    """
    Bhashini ASR and TTS for Hindi and Kannada.

    serviceIds are cached per (task, language) after first lookup. This matters:
    without the cache every voice note costs two network round trips instead of
    one, which is latency the user feels on a WhatsApp reply.
    """

    def __init__(
        self,
        udyat_key: Optional[str] = None,
        inference_key: Optional[str] = None,
        pipeline_id: str = PIPELINE_ID_MEITY,
    ) -> None:
        self.udyat_key = udyat_key or os.environ.get("BHASHINI_UDYAT_KEY", "").strip()
        self.inference_key = (
            inference_key or os.environ.get("BHASHINI_INFERENCE_KEY", "").strip()
        )
        self.pipeline_id = pipeline_id
        self._service_cache: dict[_CacheKey, str] = {}
        self._cache_lock = threading.Lock()

        missing = [
            name
            for name, value in (
                ("BHASHINI_UDYAT_KEY", self.udyat_key),
                ("BHASHINI_INFERENCE_KEY", self.inference_key),
            )
            if not value
        ]
        if missing:
            raise BhashiniConfigError(
                f"Missing Bhashini credentials: {', '.join(missing)}. "
                "Set them in .env (see .env.example) or pass udyat_key= and "
                "inference_key= explicitly. Both are under 'My Profile' at "
                "https://bhashini.gov.in."
            )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def speech_to_text(self, audio_bytes: bytes, language: LanguageCode = "hi") -> str:
        """Transcribe speech audio (WAV bytes) to text in the given language."""
        self._check_language(language)
        service_id = self.get_service_id("asr", language)
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        return self.speech_to_text_b64(audio_b64, language, service_id)

    def speech_to_text_b64(
        self,
        audio_b64: str,
        language: LanguageCode = "hi",
        service_id: Optional[str] = None,
    ) -> str:
        """Same as speech_to_text but takes already-base64 audio (avoids a re-encode)."""
        self._check_language(language)
        service_id = service_id or self.get_service_id("asr", language)
        payload = {
            "pipelineTasks": [
                {
                    "taskType": "asr",
                    "config": {
                        "language": {"sourceLanguage": language},
                        "serviceId": service_id,
                        "preProcessors": [],
                        "postProcessors": [],
                    },
                }
            ],
            "inputData": {"audio": [{"audioContent": audio_b64}]},
        }
        data = self._post(
            COMPUTE_URL,
            payload,
            {"Authorization": self.inference_key},
            _COMPUTE_TIMEOUT,
        )
        try:
            return data["pipelineResponse"][0]["output"][0]["source"]
        except (KeyError, IndexError, TypeError) as exc:
            raise BhashiniAPIError(
                "Unexpected ASR response shape (expected "
                "pipelineResponse[0].output[0].source). "
                f"Raw response: {data!r}"
            ) from exc

    def text_to_speech(
        self,
        text: str,
        language: LanguageCode = "hi",
        gender: Literal["male", "female"] = "female",
        sampling_rate: int = 8000,
    ) -> bytes:
        """Synthesise speech audio (WAV bytes) from text in the given language."""
        self._check_language(language)
        if not text.strip():
            raise ValueError("text_to_speech() got empty text")
        service_id = self.get_service_id("tts", language)
        payload = {
            "pipelineTasks": [
                {
                    "taskType": "tts",
                    "config": {
                        "language": {"sourceLanguage": language},
                        "serviceId": service_id,
                        "gender": gender,
                        "samplingRate": sampling_rate,
                    },
                }
            ],
            "inputData": {"input": [{"source": text}]},
        }
        data = self._post(
            COMPUTE_URL,
            payload,
            {"Authorization": self.inference_key},
            _COMPUTE_TIMEOUT,
        )
        try:
            audio_b64 = data["pipelineResponse"][0]["audio"][0]["audioContent"]
        except (KeyError, IndexError, TypeError) as exc:
            raise BhashiniAPIError(
                "Unexpected TTS response shape (expected "
                "pipelineResponse[0].audio[0].audioContent). "
                f"Raw response keys: {list(data) if isinstance(data, dict) else type(data)}"
            ) from exc
        return base64.b64decode(audio_b64)

    def get_service_id(self, task: TaskType, language: LanguageCode) -> str:
        """
        Resolve (and cache) the serviceId for a task + language.

        Cached because the config call is pure overhead on every subsequent
        request for the same pair.
        """
        key = _CacheKey(task, language)
        with self._cache_lock:
            cached = self._service_cache.get(key)
        if cached:
            return cached

        payload = {
            "pipelineTasks": [
                {"taskType": task, "config": {"language": {"sourceLanguage": language}}}
            ],
            "pipelineRequestConfig": {"pipelineId": self.pipeline_id},
        }
        # NOTE: only ulcaApiKey. Adding userID causes HTTP 400 (verified).
        data = self._post(
            CONFIG_URL, payload, {"ulcaApiKey": self.udyat_key}, _CONFIG_TIMEOUT
        )

        service_id = None
        for entry in data.get("pipelineResponseConfig", []):
            if entry.get("taskType") == task:
                configs = entry.get("config") or []
                if configs:
                    service_id = configs[0].get("serviceId")
                break

        if not service_id:
            raise BhashiniAPIError(
                f"No serviceId returned for task={task!r} language={language!r}. "
                f"Bhashini may not offer this combination. Raw response: {data!r}"
            )

        with self._cache_lock:
            self._service_cache[key] = service_id
        return service_id

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _check_language(language: str) -> None:
        if language not in _SUPPORTED_LANGUAGES:
            raise BhashiniUnsupportedLanguageError(
                f"Unsupported language {language!r}. Supported: "
                f"{sorted(_SUPPORTED_LANGUAGES)}. Extend _SUPPORTED_LANGUAGES "
                "only after confirming Bhashini serves that language for the task."
            )

    def _post(self, url: str, payload: dict, headers: dict, timeout: int) -> dict:
        headers = {"Content-Type": "application/json", **headers}
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.Timeout as exc:
            raise BhashiniAPIError(f"Bhashini timed out after {timeout}s: {url}") from exc
        except requests.RequestException as exc:
            raise BhashiniAPIError(f"Network error calling {url}: {exc}") from exc

        if resp.status_code in (401, 403):
            raise BhashiniConfigError(
                f"Bhashini rejected the credentials (HTTP {resp.status_code}) at {url}. "
                "Check BHASHINI_UDYAT_KEY (config call) and BHASHINI_INFERENCE_KEY "
                f"(compute call). Response: {resp.text[:300]}"
            )
        if not resp.ok:
            raise BhashiniAPIError(
                f"Bhashini returned HTTP {resp.status_code} at {url}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise BhashiniAPIError(
                f"Bhashini response was not JSON ({url}): {resp.text[:300]}"
            ) from exc
