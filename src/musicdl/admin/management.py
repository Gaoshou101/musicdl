from __future__ import annotations

import unicodedata
from copy import deepcopy
from typing import Any

from musicdl.telegram.bots import MAX_BOT_TIMEOUT, validate_bot_username, validate_command_template


class SourceManager:
    _DEFAULTS = {"enabled": True, "priority": 0, "timeout": 10.0, "name": None}
    _FIELDS = ("enabled", "priority", "timeout", "name")

    def __init__(self, sources=(), *, on_change=None):
        self._sources = {}
        self._on_change = on_change
        for item in sources:
            self.register(item)

    @classmethod
    def _coerce(cls, name: str, value: Any) -> Any:
        """Validate and normalize one editable field."""
        if name == "enabled":
            if not isinstance(value, bool):
                raise ValueError("enabled must be boolean")
            return value
        if name == "priority":
            if not isinstance(value, int) or isinstance(value, bool) or not -1000 <= value <= 1000:
                raise ValueError("invalid priority")
            return value
        if name == "timeout":
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0.1 <= value <= 300:
                raise ValueError("invalid timeout")
            return float(value)
        if name == "name":
            # An empty name clears the label; a null one never reaches here
            # because ``update`` treats null as "keep the stored value".
            if value is None or value == "":
                return None
            if not isinstance(value, str):
                raise ValueError("invalid name")
            cleaned = unicodedata.normalize("NFKC", value).strip()
            if not cleaned or len(cleaned) > 120 or not all(char.isprintable() for char in cleaned):
                raise ValueError("invalid name")
            return cleaned
        raise ValueError("invalid source configuration")

    @classmethod
    def _validated(cls, item) -> dict:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str): raise ValueError("invalid id")
        return {"id": item["id"],
                **{name: cls._coerce(name, item.get(name, cls._DEFAULTS[name])) for name in cls._FIELDS}}

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

    def update(self, source_id: str, **changes) -> dict:
        """Apply the provided fields; an omitted or null field keeps its value."""
        if source_id not in self._sources:
            raise KeyError(source_id)
        item = self._sources[source_id]
        previous = deepcopy(item)
        for name, value in changes.items():
            if name not in self._FIELDS:
                raise ValueError("invalid source configuration")
            if value is None:
                continue
            item[name] = self._coerce(name, value)
        if item != previous:
            try:
                self._notify()
            except BaseException:
                self._sources[source_id] = previous
                raise
        return deepcopy(item)

    def remove(self, source_id: str) -> dict:
        """Delete one entry; an unknown id stays a hard error."""
        if source_id not in self._sources:
            raise KeyError(source_id)
        removed = self._sources.pop(source_id)
        try:
            self._notify()
        except BaseException:
            self._sources[source_id] = removed
            raise
        return deepcopy(removed)

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


# The bots a deployment starts with. ``music_v1bot`` is the public Telegram bot
# this project is built around, so a fresh install can search through it without
# the operator defining anything first. The seed only covers a deployment that
# has never stored a bot list: an operator who deletes or renames the entry owns
# the stored list from the first save on, and that decision survives a restart.
DEFAULT_BOTS: tuple[dict[str, Any], ...] = (
    {"id": "music_v1bot", "username": "music_v1bot", "timeout": 10.0},
)


class BotManager(SourceManager):
    """Registered Telegram bots: the bounded controls plus a Bot definition.

    A definition is what the runtime needs to build a search source: the
    Telegram username and, for a custom bot, the single-placeholder command
    template. A definition without a username is rejected, because a bot the
    runtime cannot address is not a bot.

    The timeout is capped by the Telegram adapter's own limit rather than the
    generic source limit, so every definition the portal accepts is one the
    adapter can build. That is the whole point of validating here.
    """

    # A bot carries no display label: ``name`` labels an audio source, so the
    # bot definition keeps the three shared controls plus its own two fields.
    _DEFAULTS = {"enabled": True, "priority": 0, "timeout": 10.0,
                 "username": None, "command_template": None}
    _FIELDS = ("enabled", "priority", "timeout", "username", "command_template")

    @classmethod
    def _coerce(cls, name: str, value: Any) -> Any:
        if name == "timeout":
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not 0.1 <= value <= MAX_BOT_TIMEOUT):
                raise ValueError("invalid timeout")
            return float(value)
        if name == "username":
            return validate_bot_username(value)
        if name == "command_template":
            # An empty template selects the built-in public ``/search {query}``
            # contract, which is how an operator switches a custom bot back.
            return None if value is None or value == "" else validate_command_template(value)
        return super()._coerce(name, value)
