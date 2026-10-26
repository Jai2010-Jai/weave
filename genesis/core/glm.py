"""GLM (Zhipu / Z.ai) behind the same `client.models` calls GeminiClient makes with google-genai.

With GENESIS_AI_PROVIDER=glm, GeminiClient talks to this (through RoutedModels, which still sends Gemini model
names to Gemini) instead of Gemini directly, so its retries, model fallback, chat racing and schema checks work
unchanged. GLM speaks the OpenAI chat format: the system prompt becomes a system
message, a JSON schema is spelled out in it (GLM's JSON mode takes no schema), and thinking is turned off so
replies come back in seconds."""

from __future__ import annotations

import json
import time
import types

import httpx

# GLM error codes that mean "busy, try again or try another model" (the HTTP status is 429 for all of them).
BUSY_CODES = {"1302", "1303", "1305", "1312"}
MAX_TOKENS = 32768
# The free glm-4.7-flash answers "overloaded" about half the time and a moment later answers fine, so a busy answer
# is asked again after these pauses (seconds) before it counts as a failure.
BUSY_RETRIES = (0.5, 1, 2, 3, 5)


class GlmError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.code = status


class GlmModels:
    def __init__(self, api_key: str, base_url: str, models: list[str], timeout: float = 600):
        self.models = models
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout,
                                  headers={"Authorization": f"Bearer {api_key}"})

    def list(self, config=None):
        return [types.SimpleNamespace(name=f"models/{m}", supported_actions=["generateContent"]) for m in self.models]

    def generate_content(self, model: str, contents: str, config):
        system = getattr(config, "system_instruction", None) or ""
        schema = getattr(config, "response_json_schema", None)
        if schema:
            system += ("\n\nReply with one JSON object only (no markdown fences) that matches this JSON schema:\n"
                       + json.dumps(schema))
        body = {
            "model": model,
            "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": contents}],
            "max_tokens": min(getattr(config, "max_output_tokens", None) or MAX_TOKENS, MAX_TOKENS),
            "thinking": {"type": "disabled"},
        }
        if getattr(config, "temperature", None) is not None:
            body["temperature"] = config.temperature
        if schema or getattr(config, "response_mime_type", None) == "application/json":
            body["response_format"] = {"type": "json_object"}
        for pause in (*BUSY_RETRIES, None):
            try:
                r = self._http.post("/chat/completions", json=body)
            except httpx.HTTPError as e:
                raise GlmError(503, f"UNAVAILABLE: GLM request failed: {type(e).__name__}") from e
            if r.status_code == 200:
                break
            err = _error(r)
            if err.code != 503 or pause is None:
                raise err
            time.sleep(pause)
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage") or {}
        return types.SimpleNamespace(
            text=(choice.get("message") or {}).get("content") or "",
            usage_metadata=types.SimpleNamespace(prompt_token_count=usage.get("prompt_tokens"),
                                                 candidates_token_count=usage.get("completion_tokens")),
            candidates=[types.SimpleNamespace(finish_reason=choice.get("finish_reason"))])


class RoutedModels:
    """glm-* models go to GLM and every other model to Gemini, so one list of models can mix the two: GLM first,
    Gemini taking over when GLM is busy or out of quota."""

    def __init__(self, glm: GlmModels, gemini=None):
        self.glm = glm
        self.gemini = gemini  # google-genai `client.models`, or None without a Gemini key

    def list(self, config=None):
        return self.glm.list() + (list(self.gemini.list(config=config)) if self.gemini is not None else [])

    def generate_content(self, model: str, contents: str, config):
        if model.startswith("glm"):
            return self.glm.generate_content(model=model, contents=contents, config=config)
        if self.gemini is None:
            raise GlmError(400, f"{model} is a Gemini model, but GEMINI_API_KEY is not set")
        return self.gemini.generate_content(model=model, contents=contents, config=config)


def _error(r: httpx.Response) -> GlmError:
    try:
        err = r.json().get("error") or {}
    except ValueError:
        err = {}
    code, message = str(err.get("code", "")), str(err.get("message") or r.text)[:300]
    if code in BUSY_CODES or r.status_code >= 500:
        return GlmError(503, f"UNAVAILABLE: GLM is overloaded ({code or r.status_code}): {message}")
    if r.status_code == 429:
        return GlmError(429, f"RESOURCE_EXHAUSTED: GLM quota or balance ({code}): {message}")
    return GlmError(r.status_code, f"GLM request failed ({r.status_code} {code}): {message}")
