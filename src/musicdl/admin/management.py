from __future__ import annotations

from copy import deepcopy
from typing import Any


class SourceManager:
    def __init__(self, sources=(), *, on_change=None):
        self._sources = {}
        self._on_change = on_change
        for item in sources:
            self.register(item)

    @staticmethod
    def _validated(item) -> dict:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str): raise ValueError("invalid id")
        enabled, priority, timeout = item.get("enabled", True), item.get("priority", 0), item.get("timeout", 10.0)
        if not isinstance(enabled, bool) or not isinstance(priority, int) or isinstance(priority, bool) or not -1000 <= priority <= 1000 or not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0.1 <= timeout <= 300: raise ValueError("invalid source configuration")
        return {"id": item["id"], "enabled": enabled, "priority": priority, "timeout": float(timeout)}

    def register(self, item, *, persist: bool = False) -> dict:
        """Add one bounded entry; an existing id stays a hard error.

        ``persist`` stays off while the runtime publishes the sources it
        assembled, so a restart cannot overwrite an operator's own choices.
        """
        entry = self._validated(item)
        if entry["id"] in self._sources: raise ValueError("duplicate id")
        self._sources[entry["id"]] = entry
        if persist: self._notify()
        return deepcopy(entry)

    def list(self) -> list[dict]:
        return sorted((deepcopy(item) for item in self._sources.values()), key=lambda x: (x["priority"], x["id"]))

    def update(self, source_id: str, *, enabled: bool | None = None, priority: int | None = None, timeout: float | None = None) -> dict:
        if source_id not in self._sources:
            raise KeyError(source_id)
        item = self._sources[source_id]
        previous = deepcopy(item)
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
        if item != previous:
            try:
                self._notify()
            except BaseException:
                self._sources[source_id] = previous
                raise
        return deepcopy(item)

    def snapshot(self) -> list[dict]:
        return [deepcopy(item) for item in self._sources.values()]

    def restore(self, entries) -> None:
        """Adopt persisted entries, skipping any that no longer validate."""
        if not isinstance(entries, list):
            return
        for item in entries:
            try:
                self.register(item)
            except ValueError:
                continue

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change()


class BotManager(SourceManager):
    """Configuration manager with the same bounded controls for registered bots."""
