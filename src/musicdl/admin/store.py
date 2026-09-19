"""Durable snapshot of the administration control plane."""

from __future__ import annotations

import json
import os
from pathlib import Path


class AdminStateError(RuntimeError):
    """Raised when a configured state file cannot be trusted."""


class AdminStateStore:
    """One JSON file holding the administrator's own changes.

    The control plane has a single writer, so the file is replaced atomically
    rather than merged. Without a path every method is a no-op, which keeps
    local runs and tests on the in-memory behaviour they had before.

    A missing file is a first run and yields defaults. A file that exists but
    cannot be read, parsed, or version-matched raises instead of falling back
    to the default credentials, because silently reverting a changed password
    to ``admin``/``password`` would be worse than refusing to start.
    """

    VERSION = 1

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path else None

    def load(self) -> dict:
        if self.path is None or not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise AdminStateError(f"administrator state file is unreadable: {self.path}") from None
        if not isinstance(payload, dict) or payload.get("version") != self.VERSION:
            raise AdminStateError(f"unsupported administrator state file: {self.path}")
        return payload

    def save(self, *, credentials: dict, sources: list, bots: list, settings: dict | None = None) -> None:
        if self.path is None:
            return
        document = {"version": self.VERSION, "credentials": credentials,
                    "sources": sources, "bots": bots, "settings": settings or {}}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(document, sort_keys=True))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
