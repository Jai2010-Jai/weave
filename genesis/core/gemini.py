"""Gemini integration: model discovery, app generation and build repair, with structured output."""

from __future__ import annotations

import json
import logging
import threading
import time
from types import SimpleNamespace
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from pydantic import BaseModel, ValidationError as PydanticValidationError

from ..config import Settings
from . import prompts
from .health import ModelHealth
from .schemas import AppManifest, RepairPatch

log = logging.getLogger("genesis.gemini")

# Preference order for GEMINI_MODEL=auto. Aliases and modern fast models first.
PREFERRED_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-latest",
    "gemini-3.8-flash",  # slowest measured (minutes per call under load)
    "gemini-flash-lite-latest",
    "gemini-pro-latest",
]


# Interactive requests (chat, translation) race a second model when the first is slow; abandoned calls finish here.
_HEDGE_POOL = ThreadPoolExecutor(max_workers=32, thread_name_prefix="gemini-hedge")


def thinking_config(model: str, level: str | None):
    """The cheapest thinking setting each model family accepts: 2.5 models take a token budget, newer ones a level."""
    from google.genai import types

    if not level:
        return None
    if model.startswith("gemini-2.5-flash"):
        return types.ThinkingConfig(thinking_budget=0)
    if model.startswith("gemini-2.5"):
        return types.ThinkingConfig(thinking_budget=128)
    return types.ThinkingConfig(thinking_level=level)


class GeminiError(RuntimeError):
    """A Gemini call failed. The message is safe to show to the user (never contains the key)."""


class GeminiClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._client = None
        self._model: str | None = None
        self._available_models: set[str] | None = None
        self._lock = threading.RLock()
        self.health = ModelHealth(settings.data_dir / "gemini-health.json")
        self._no_thinking: set[str] = set()  # models that rejected a thinking setting

    @property
    def configured(self) -> bool:
        return bool(self.s.gemini_api_key) or (self.s.ai_provider == "glm" and bool(self.s.glm_api_key))

    def _get_client(self):
        if not self.configured:
            raise GeminiError("GEMINI_API_KEY is not set. Add it to .env (see README) and restart Genesis.")
        if self._client is None and self.s.ai_provider == "glm":
            from .glm import GlmModels, RoutedModels

            glm_models = [m for m in [self.s.glm_model, *self.s.website_fallback_models.split(",")] if m.startswith("glm")]
            gemini = None
            if self.s.gemini_api_key:
                from google import genai

                gemini = genai.Client(api_key=self.s.gemini_api_key).models
            glm = GlmModels(self.s.glm_api_key, self.s.glm_base_url, glm_models)
            self._client = SimpleNamespace(models=RoutedModels(glm, gemini))
        if self._client is None:
            from google import genai  # imported lazily so tests run without network

            self._client = genai.Client(api_key=self.s.gemini_api_key)
        return self._client

    def model_name(self) -> str:
        with self._lock:
            if self._model:
                return self._model
            requested = self.s.gemini_model
            available = self.list_models()
            if requested and requested != "auto":
                if requested not in available:
                    raise GeminiError(f"GEMINI_MODEL={requested!r} is not available to this API key. "
                                      f"Available: {', '.join(sorted(available)[:15])}")
                self._model = requested
            else:
                self._model = next((m for m in PREFERRED_MODELS if m in available), None)
                if not self._model:
                    raise GeminiError("No suitable Gemini text model is available to this API key.")
            log.info("Using Gemini model %s", self._model)
            return self._model

    def list_models(self, force_refresh: bool = False) -> set[str]:
        with self._lock:
            if self._available_models is not None and not force_refresh:
                return self._available_models
            client = self._get_client()
            try:
                names = set()
                for m in client.models.list(config={"page_size": 200}):
                    actions = getattr(m, "supported_actions", None) or []
                    if "generateContent" in actions:
                        names.add(m.name.removeprefix("models/"))
                self._available_models = names
                return names
            except Exception as e:  # noqa: BLE001 - surface a safe message
                raise GeminiError(f"Could not list Gemini models: {_safe_error(e)}") from e

    # ---------- calls ----------
    def generate_app(self, prompt: str, app_name: str | None) -> tuple[AppManifest, dict]:
        return self._structured(prompts.SYSTEM, prompts.generation_prompt(prompt, app_name), AppManifest)

    def modify_app(self, change: str, files: dict[str, str], manifest: AppManifest) -> tuple[AppManifest, dict]:
        return self._structured(prompts.SYSTEM, prompts.modification_prompt(change, files, manifest), AppManifest)

    def repair(self, diagnostics: str, files: dict[str, str], manifest: AppManifest) -> tuple[RepairPatch, dict]:
        return self._structured(prompts.SYSTEM, prompts.repair_prompt(diagnostics, files, manifest), RepairPatch)

    def ask(self, system: str, user: str, schema: type[BaseModel], title: str = "", timeout: int | None = None):
        """One structured request with a custom system prompt (used by the chat brain and NLG, on GENESIS_CHAT_MODEL if set).
        Minimal thinking: a chat reply has to arrive in seconds, and these are short routing decisions."""
        primary = self.s.chat_model or self.model_name()
        models = [primary] + [m for m in [self.s.chat_fallback_model] if m and m != primary]
        return self._structured(system, user, schema, model=primary, timeout=timeout, thinking="minimal",
                                hedge_models=models)

    def _structured(self, system: str, user: str, schema: type[BaseModel], attempts: int = 2,
                    model: str | None = None, timeout: int | None = None, thinking: str | None = None,
                    hedge_models: list[str] | None = None):
        from google.genai import types

        client = self._get_client()
        model = model or self.model_name()
        last_err = ""
        for attempt in range(1, attempts + 1):
            contents = user if attempt == 1 else (
                f"{user}\n\nYour previous reply was rejected: {last_err}\nReturn ONLY valid JSON matching the schema.")
            started = time.monotonic()
            config = types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_json_schema=schema.model_json_schema(),
                temperature=0.4,
                max_output_tokens=60000,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                thinking_config=thinking_config(model, thinking),
            )
            if hedge_models:
                resp, model = self._hedged(client, hedge_models, contents, config, thinking)
            else:
                resp, model = self._call_with_retry(client, model, contents, config, timeout=timeout)
            meta = {
                "model": model,
                "seconds": round(time.monotonic() - started, 1),
                "attempt": attempt,
            }
            usage = getattr(resp, "usage_metadata", None)
            if usage is not None:
                meta["input_tokens"] = getattr(usage, "prompt_token_count", None)
                meta["output_tokens"] = getattr(usage, "candidates_token_count", None)
            text = (resp.text or "").strip()
            if not text:
                reason = ""
                if resp.candidates:
                    reason = str(getattr(resp.candidates[0], "finish_reason", ""))
                last_err = f"empty response {reason}".strip()
                continue
            try:
                return schema.model_validate_json(_strip_fences(text)), meta
            except PydanticValidationError as e:
                last_err = _compact_validation_error(e)
                log.warning("Gemini output failed schema validation: %s", last_err)
            except json.JSONDecodeError as e:
                last_err = f"invalid JSON: {e}"
        raise GeminiError(f"Gemini did not return a valid {schema.__name__}: {last_err}")

    def _hedged(self, client, models: list[str], contents: str, config, thinking: str | None):
        """Ask the first model; if it hasn't answered within chat_hedge_seconds (or fails), ask the next one too and
        use whichever answers first. For chat, a fast answer matters more than which model gave it."""
        models = self.health.order(models)
        pending: dict = {}
        last: Exception | None = None
        queue = list(models)

        def start(m: str) -> None:
            cfg = config.model_copy(update={"thinking_config": thinking_config(m, thinking)})
            if m in self._no_thinking:
                cfg = cfg.model_copy(update={"thinking_config": None})
            pending[_HEDGE_POOL.submit(self._one_call, client, m, contents, cfg)] = m

        start(queue.pop(0))
        while pending:
            done, _ = wait(list(pending), timeout=self.s.chat_hedge_seconds if queue else None,
                           return_when=FIRST_COMPLETED)
            if not done:
                log.info("Gemini %s slow; also asking %s", ", ".join(pending.values()), queue[0])
                start(queue.pop(0))
                continue
            for f in done:
                m = pending.pop(f)
                try:
                    resp = f.result()
                except Exception as e:  # noqa: BLE001
                    last = e
                    if _is_transient(e):
                        self.health.mark(m, True)
                    if queue:
                        start(queue.pop(0))
                    continue
                self.health.mark(m, False)
                return resp, m
        raise GeminiError(f"Gemini is temporarily unavailable, please retry: {_safe_error(last)}") from last

    def _one_call(self, client, model: str, contents: str, cfg):
        try:
            return client.models.generate_content(model=model, contents=contents, config=cfg)
        except Exception as e:
            if cfg.thinking_config is not None and "think" in str(e).lower() and not _is_transient(e):
                self._no_thinking.add(model)
                return client.models.generate_content(
                    model=model, contents=contents, config=cfg.model_copy(update={"thinking_config": None}))
            raise

    def _call_with_retry(self, client, model: str, contents: str, config, timeout: int | None = None):
        """Retry transient overload errors with backoff; in auto mode fall back to the next model."""
        candidates = [model]
        if self.s.gemini_model == "auto":
            try:
                available = self.list_models()
            except GeminiError:
                available = set()
            candidates += [m for m in PREFERRED_MODELS if m != model and m in available][:2]
        elif self.s.ai_provider == "glm":
            candidates += [m for m in self.s.website_fallback_models.split(",") if m and m != model]
        candidates = self.health.order(candidates)
        last: Exception | None = None
        # Fast bounded backoff for interactive requests:
        delays = (0, 1, 3) if (timeout and timeout <= 30) else (0, 2, 6)
        for m in candidates:
            # GLM calls already retry busy answers themselves; a GLM failure moves straight on to the next model.
            for delay in (0,) if m.startswith("glm") else delays:
                if delay:
                    log.warning("Gemini transient error on %s; retrying in %ss", m, delay)
                    time.sleep(delay)
                cfg = config
                if m in self._no_thinking and getattr(config, "thinking_config", None) is not None:
                    cfg = config.model_copy(update={"thinking_config": None})
                try:
                    resp = client.models.generate_content(model=m, contents=contents, config=cfg)
                    self.health.mark(m, False)
                    return resp, m
                except Exception as e:  # noqa: BLE001
                    last = e
                    if cfg.thinking_config is not None and "think" in str(e).lower() and not _is_transient(e):
                        # This model takes no thinking setting: ask again without one.
                        self._no_thinking.add(m)
                        try:
                            resp = client.models.generate_content(
                                model=m, contents=contents, config=config.model_copy(update={"thinking_config": None}))
                            return resp, m
                        except Exception as e2:  # noqa: BLE001
                            last = e = e2
                    if _is_quota(e):
                        break  # this model's quota is used up; the next model has its own
                    if not _is_transient(e):
                        raise GeminiError(f"Gemini request failed: {_safe_error(e)}") from e
            self.health.mark(m, True)
        raise GeminiError(f"Gemini is temporarily unavailable, please retry: {_safe_error(last)}") from last


def _is_transient(e: Exception) -> bool:
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if isinstance(code, int) and code in (500, 502, 503, 504):
        return True
    text = str(e).upper()
    return any(t in text for t in ("503", "UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL", "TIMED OUT"))


def _is_quota(e: Exception) -> bool:
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    return code == 429 or "RESOURCE_EXHAUSTED" in str(e).upper()


def _strip_fences(text: str) -> str:
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _compact_validation_error(e: PydanticValidationError) -> str:
    parts = []
    for err in e.errors()[:6]:
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def _safe_error(e: Exception) -> str:
    msg = str(e)
    # Defensive: never echo anything that looks like a credential.
    for marker in ("key=", "x-goog-api-key"):
        if marker in msg:
            msg = msg.split(marker)[0] + "[redacted]"
    hint = ""
    low = msg.lower()
    if "permission" in low or "api key not valid" in low or "401" in low or "403" in low:
        hint = " (check that GEMINI_API_KEY is valid and the Generative Language API is enabled)"
    elif "429" in low or "quota" in low or "resource_exhausted" in low:
        hint = " (quota exhausted: free-tier limits apply unless billing is enabled on the Google Cloud project)"
    return msg[:600] + hint
