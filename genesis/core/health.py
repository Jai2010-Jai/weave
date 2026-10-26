"""Which Gemini models were overloaded recently, shared by every build and chat reply (the website bridge reads
and writes the same JSON file). A model that answered "503 high demand" in the last few minutes is tried last,
so each request doesn't lose time rediscovering it."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

TTL_S = 600


class ModelHealth:
    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def busy(self, model: str) -> bool:
        stamp = self._read().get(model) or 0
        return isinstance(stamp, (int, float)) and time.time() * 1000 - stamp < TTL_S * 1000

    def mark(self, model: str, busy: bool) -> None:
        h = self._read()
        if not busy and not h.get(model):
            return
        h[model] = int(time.time() * 1000) if busy else 0
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(h))
            tmp.replace(self.path)
        except OSError:
            pass  # only an ordering hint

    def order(self, models: list[str]) -> list[str]:
        """Healthy models first, recently overloaded ones last; otherwise the given order."""
        return sorted(models, key=self.busy)
