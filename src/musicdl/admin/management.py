from __future__ import annotations

from copy import deepcopy
from typing import Any


class SourceManager:
    def __init__(self, sources=()):
        self._sources = {}
        for item in sources:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str): raise ValueError("invalid id")
            enabled, priority, timeout = item.get("enabled", True), item.get("priority", 0), item.get("timeout", 10.0)
            if not isinstance(enabled, bool) or not isinstance(priority, int) or isinstance(priority, bool) or not -1000 <= priority <= 1000 or not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0.1 <= timeout <= 300: raise ValueError("invalid source configuration")
            self._sources[item["id"]] = {"id": item["id"], "enabled": enabled, "priority": priority, "timeout": float(timeout)}

    def list(self) -> list[dict]:
        return sorted((deepcopy(item) for item in self._sources.values()), key=lambda x: (x["priority"], x["id"]))

    def update(self, source_id: str, *, enabled: bool | None = None, priority: int | None = None, timeout: float | None = None) -> dict:
        if source_id not in self._sources:
            raise KeyError(source_id)
        item = self._sources[source_id]
        if enabled is not None:
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be boolean")
            item["enabled"] = enabled
        if priority is not None:
            if not isinstance(priority, int) or isinstance(priority, bool) or not -1000 <= priority <= 1000:
                raise ValueError("invalid priority")
            item["priority"] = priority
        if timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0.1 <= timeout <= 300):
            raise ValueError("invalid timeout")
        if timeout is not None:
            item["timeout"] = float(timeout)
        return deepcopy(item)


class BotManager(SourceManager):
    """Configuration manager with the same bounded controls for registered bots."""
