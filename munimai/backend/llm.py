"""
The reasoning model, behind a thin OpenAI-compatible adapter.

Why an adapter and not the openai SDK directly: the model must be swappable
without touching agent code. Today it is Qwen on vLLM over Tailscale; tomorrow
it could be a local process or a different served model. Agents depend on this
interface, never on a URL.

Every call is timed and (if a Trace is passed) recorded, so the glass box shows
what the model was asked and what it answered.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

from .config import LLMSettings
from .trace import Trace


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int


class LLM:
    def __init__(self, settings: Optional[LLMSettings] = None) -> None:
        self.s = settings or LLMSettings.from_env()

    # ------------------------------------------------------------------ chat
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 800,
        trace: Optional[Trace] = None,
        agent: str = "llm",
        stop: Optional[list[str]] = None,
    ) -> LLMResult:
        body: dict[str, Any] = {
            "model": self.s.model_served_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if stop:
            body["stop"] = stop

        if trace is not None:
            trace.emit(
                agent, "llm_call",
                {"messages": messages, "temperature": temperature,
                 "max_tokens": max_tokens},
            )

        start = time.perf_counter()
        try:
            resp = requests.post(
                f"{self.s.base_url}/chat/completions",
                json=body, timeout=self.s.timeout_s,
            )
        except requests.RequestException as exc:
            raise LLMError(
                f"Cannot reach the model at {self.s.base_url}. Is the vLLM "
                f"server up on the supercomputer? ({exc})"
            ) from exc

        latency_ms = int((time.perf_counter() - start) * 1000)
        if not resp.ok:
            raise LLMError(f"Model returned HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected model response: {data!r}") from exc

        usage = data.get("usage", {})
        result = LLMResult(
            text=text,
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            latency_ms=latency_ms,
        )
        if trace is not None:
            trace.emit(
                agent, "llm_call",
                {"response": text[:2000]},
                latency_ms=latency_ms,
                tokens_in=result.tokens_in, tokens_out=result.tokens_out,
            )
        return result

    # ------------------------------------------------------------- json call
    def json_call(
        self,
        system: str,
        user: str,
        *,
        schema_hint: str,
        trace: Optional[Trace] = None,
        agent: str = "llm",
        max_tokens: int = 500,
        retries: int = 1,
    ) -> dict:
        """
        Ask for a JSON object and parse it. Qwen scored 0.90 on this in the
        capability test, but not 1.0, so we strip code fences and retry once
        with a corrective nudge rather than trusting the first token stream.
        """
        messages = [
            {"role": "system",
             "content": f"{system}\n\nRespond with ONLY a valid JSON object "
                        f"matching this shape: {schema_hint}. No prose, no code fences."},
            {"role": "user", "content": user},
        ]
        last_err = ""
        for attempt in range(retries + 1):
            result = self.chat(messages, max_tokens=max_tokens, trace=trace, agent=agent)
            parsed = _extract_json(result.text)
            if parsed is not None:
                return parsed
            last_err = result.text[:200]
            messages.append({"role": "assistant", "content": result.text})
            messages.append({"role": "user",
                             "content": "That was not valid JSON. Return ONLY the "
                                        "JSON object, nothing else."})
        raise LLMError(f"Model did not return valid JSON after {retries + 1} tries. "
                       f"Last output: {last_err}")

    def health(self) -> bool:
        try:
            r = requests.get(f"{self.s.base_url}/models", timeout=10)
            return r.ok
        except requests.RequestException:
            return False


def _extract_json(text: str) -> Optional[dict]:
    """Pull a JSON object out of a model response, tolerating code fences."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    t = t.strip()
    # find the outermost object
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(t[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
