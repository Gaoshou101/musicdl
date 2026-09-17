from __future__ import annotations

import dataclasses
from urllib.parse import urlsplit, urlunsplit
from musicdl.secrets import redact_secrets
from collections.abc import Awaitable, Callable
from musicdl.media.models import DownloadEvent


class HealthAggregator:
    def __init__(self, probes: dict[str, Callable[[], Awaitable[bool | None]]]):
        self.probes = probes

    async def check(self) -> dict:
        checks = {}
        for name in ("readyz", "redis", "plugin_runner", "telegram"):
            probe = self.probes.get(name)
            if probe is None: checks[name] = "unavailable"; continue
            try:
                outcome = await probe()
            except Exception:
                checks[name] = "unavailable"
                continue
            # ``None`` is the probe saying this deployment has nothing for that
            # dependency to do -- Redis without WeCom, the plugin runner before
            # a source needs it.  Reporting that as a failure would keep a
            # working deployment red on the panel for its whole life.
            checks[name] = "not_required" if outcome is None else ("ok" if outcome else "failed")
        healthy = all(value in ("ok", "not_required") for value in checks.values())
        return {"status": "ok" if healthy else "degraded", "checks": checks}


class EventLogStore:
    def __init__(self):
        self._events: list[dict] = []

    def append(self, event: DownloadEvent | dict) -> None:
        data = dataclasses.asdict(event) if dataclasses.is_dataclass(event) else dict(event)
        self._events.append(_redact_urls(redact_secrets(data)))

    def page(self, *, offset: int = 0, limit: int = 50) -> dict:
        if offset < 0 or limit < 1 or limit > 200: raise ValueError("invalid pagination")
        return {"items": self._events[offset:offset + limit], "total": len(self._events), "offset": offset, "limit": limit}


class AuditLogStore(EventLogStore):
    """Same bounded, redacted store used for administrator audit records."""


def _redact_urls(value):
    if isinstance(value, str) and (value.startswith("http://") or value.startswith("https://")):
        parts = urlsplit(value); return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    if isinstance(value, dict): return {k: _redact_urls(v) for k, v in value.items()}
    if isinstance(value, list): return [_redact_urls(v) for v in value]
    return value
