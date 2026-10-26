"""Translates Weave's own messages (build ready, status, questions) into each person's language before they go out.

A message for someone who chose Hindi, Telugu, Kannada or Tamil is stored as 'localizing', translated in the
background (one person's messages in order, different people in parallel) and then released to the outbox. Links,
build IDs and command words are swapped for markers before translation and put back after, so the model can't
change them; if anything goes wrong the English text is sent instead. Chat replies from the brain are already in
the right language and skip this."""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

from ..config import Settings
from .language import language_label

log = logging.getLogger("genesis.sms")

# Links, build IDs, @mentions-like codes and UPPERCASE command words (STATUS, LINK, YES) stay exactly as written.
PROTECT = re.compile(r"https?://\S+|\b[\w.-]+\.askweave\.xyz\S*|\bG-[A-Z0-9]{5}\b|\b[A-Z][A-Z0-9]{2,}\b")
SYSTEM = """You translate WhatsApp messages sent by Weave, a friendly assistant that builds apps and websites,
into {language}. Keep the meaning, the friendly casual texting tone, emoji and line breaks. Translate naturally,
the way a person would text it, not word for word. Every marker like [[0]] stands for a link, ID or command:
copy each marker exactly once and never translate, remove or add markers. Reply with JSON only."""


class Translation(BaseModel):
    text: str = Field(max_length=4000)


class Translator:
    def __init__(self, settings: Settings, ai, store, *, synchronous: bool = False):
        self.s = settings
        self.ai = ai
        self.store = store
        self._pool = None if synchronous else ThreadPoolExecutor(max_workers=8, thread_name_prefix="weave-translate")
        self._locks: dict = {}
        self._guard = threading.Lock()
        self._cache: OrderedDict[tuple[str, str], str] = OrderedDict()

    def submit(self, msg_id: int, user_id: int, text: str, code: str, fit) -> None:
        """fit: trims the translated text for the channel (M.fit with the right limit, keeping the script)."""
        def run() -> None:
            body = fit(text)
            try:
                body = fit(self.translate(text, code))
            except Exception:  # noqa: BLE001 - the English text still goes out
                log.exception("translation failed")
            self.store.release_outbound(msg_id, body)

        if self._pool is None:
            run()
            return
        with self._guard:
            lock = self._locks.setdefault(user_id, threading.Lock())

        def ordered() -> None:
            with lock:
                run()

        self._pool.submit(ordered)

    def translate(self, text: str, code: str) -> str:
        key = (code, text)
        with self._guard:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        kept: list[str] = []

        def mark(m: re.Match) -> str:
            kept.append(m.group(0))
            return f"[[{len(kept) - 1}]]"

        masked = PROTECT.sub(mark, text)
        out, _ = self.ai.ask(SYSTEM.replace("{language}", language_label(code)), masked, Translation,
                             title="Weave translate", timeout=self.s.chat_timeout)
        result = out.text.strip()
        for i, original in enumerate(kept):
            if result.count(f"[[{i}]]") != 1:
                raise ValueError(f"translation lost marker {i}")
            result = result.replace(f"[[{i}]]", original)
        if not result or "[[" in result:
            raise ValueError("bad translation")
        with self._guard:
            self._cache[key] = result
            while len(self._cache) > 500:
                self._cache.popitem(last=False)
        return result
