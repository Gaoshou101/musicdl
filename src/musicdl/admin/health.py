from __future__ import annotations

import dataclasses
from collections import deque
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


# The statuses a search can report for one source. ``ok`` is the only one that
# means the channel answered; an empty result set is still a working channel.
SEARCH_OK = "ok"

# The stages of a download attempt that count against a channel, and the counter
# each one feeds. ``cleanup`` failing is this process failing to remove a
# temporary file, not the channel misbehaving, so it is deliberately absent.
_ATTEMPT_STAGES = {"download": "downloads", "refresh": "refreshes"}


class SourceHealthStore:
    """What each source did last, rolled up for the panel's channel view.

    The event log is a stream of individual attempts -- the right thing to read
    one at a time, and the wrong thing to ask "is this channel working". This
    keeps the per-source answer instead: the last search, the last download, the
    last health probe, and a bounded window of outcomes to turn into a rate.

    Both of the panel's own paths feed it -- a search reports every source it
    queried, a download reports the events the media pipeline emits -- so a
    channel that has been exercised once is already answerable, and one that has
    not says ``unknown`` rather than pretending to be healthy.

    Memory is bounded by the number of sources, not by traffic: the window is a
    fixed number of outcomes per source, and the number of tracked ids is
    capped, so a deployment that churns through source ids cannot grow it
    without limit.
    """

    WINDOW = 20
    LIMIT = 200

    def __init__(self, window: int = WINDOW, limit: int = LIMIT) -> None:
        if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= 200:
            raise ValueError("invalid window")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("invalid limit")
        self.window = window
        self.limit = limit
        self._records: dict[str, dict] = {}

    # -- recording -------------------------------------------------------
    def observe_search(self, source_id: str, status: str, *, count: int = 0) -> None:
        """Record one source's answer to one search the panel just ran."""
        record = self._record(source_id)
        if record is None:
            return
        record["searches"] += 1
        record["last_search"] = str(status)
        record["last_count"] = max(0, int(count)) if isinstance(count, int) else 0
        record["outcomes"].append(str(status) == SEARCH_OK)
        if str(status) != SEARCH_OK:
            record["last_error"] = f"search_{status}"
            record["last_error_stage"] = "search"

    def observe_event(self, event: DownloadEvent | dict) -> None:
        """Record one event the media pipeline emitted for a source."""
        data = dataclasses.asdict(event) if dataclasses.is_dataclass(event) else dict(event)
        source_id = data.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            return
        stage = data.get("stage")
        if stage != "health" and stage not in _ATTEMPT_STAGES:
            return
        record = self._record(source_id)
        if record is None:
            return
        status = str(data.get("status"))
        if stage == "health":
            # A probe answers a different question than an attempt: whether the
            # source is up right now, which is worth showing beside the rate
            # rather than folding into it.
            record["last_health_status"] = status
            record["last_health"] = data.get("healthy") if status == "success" else None
            if status == "failed":
                record["last_error"] = data.get("error_code") or "health_failed"
                record["last_error_stage"] = "health"
            return
        record[_ATTEMPT_STAGES[stage]] += 1
        record[f"last_{stage}"] = status
        record["outcomes"].append(status == "success")
        if status != "success":
            record["last_error"] = data.get("error_code") or f"{stage}_failed"
            record["last_error_stage"] = stage

    def _record(self, source_id: str) -> dict | None:
        record = self._records.get(source_id)
        if record is not None:
            return record
        if len(self._records) >= self.limit:
            return None
        record = {"searches": 0, "downloads": 0, "refreshes": 0,
                  "last_search": None, "last_download": None, "last_refresh": None,
                  "last_count": 0, "last_error": None, "last_error_stage": None,
                  "last_health": None, "last_health_status": None,
                  "outcomes": deque(maxlen=self.window)}
        self._records[source_id] = record
        return record

    # -- reading ---------------------------------------------------------
    def snapshot(self, sources: list[dict] | None = None) -> dict:
        """The configured sources, each with what it last did.

        A configured source that has not been exercised is still listed, with
        ``unknown`` as its verdict: the panel should show every channel it could
        use, not only the ones that happen to have traffic. A source that has
        traffic but is no longer configured is kept and marked ``configured:
        false``, because a channel that was just removed is exactly when its
        last error is worth reading.
        """
        rows: list[dict] = []
        seen: set[str] = set()
        for item in sources or []:
            source_id = item.get("id")
            if not isinstance(source_id, str) or not source_id or source_id in seen:
                continue
            seen.add(source_id)
            rows.append(self._view(source_id, configured=True, name=item.get("name"),
                                   enabled=bool(item.get("enabled", True)),
                                   priority=item.get("priority")))
        for source_id in sorted(set(self._records) - seen):
            rows.append(self._view(source_id, configured=False, name=None, enabled=False, priority=None))
        rows.sort(key=lambda row: (not row["configured"], row["priority"] if row["priority"] is not None else 0,
                                   row["id"]))
        return {"sources": rows, "window": self.window, "total": len(rows)}

    def preference(self, source_id: str) -> int:
        """One channel's observed standing, as a search tie-break weight.

        Two channels offering the same recording have to be told apart, and an
        operator's declared priority cannot do it when they never ranked the
        two. What this process measured can: the recent outcome window says how
        often the channel answered, and the last search and download say
        whether it answered just now.

        The scale is deliberately small and bounded. ``0`` is the answer for an
        unknown channel, an unreadable roll-up and a channel that has not been
        exercised yet, so a deployment that has run no search keeps the order
        it had before, and a few points of rate never outweigh a priority an
        operator set on purpose.
        """
        try:
            return self._preference(source_id)
        except Exception:
            # A tie-break must never be the reason a search or a download
            # fails; a roll-up that cannot be read simply has no opinion.
            return 0

    def _preference(self, source_id: str) -> int:
        if not isinstance(source_id, str) or not source_id:
            return 0
        record = self._records.get(source_id)
        if record is None:
            return 0
        outcomes = list(record["outcomes"])
        score = round(10 * sum(1 for ok in outcomes if ok) / len(outcomes)) if outcomes else 0
        if record["last_download"] == "success":
            score += 5
        elif record["last_download"] == "failed":
            score -= 5
        if record["last_search"] is not None and record["last_search"] != SEARCH_OK:
            score -= 5
        return max(-20, min(20, score))

    def _view(self, source_id: str, *, configured: bool, name, enabled: bool, priority) -> dict:
        record = self._records.get(source_id)
        common = {"id": source_id, "name": name, "enabled": enabled, "priority": priority,
                  "configured": configured}
        if record is None:
            return dict(common, status="unknown", attempts=0, successes=0, failures=0, success_rate=None,
                        searches=0, downloads=0, refreshes=0, last_search=None, last_download=None,
                        last_refresh=None, last_count=0, last_error=None, last_error_stage=None,
                        last_health=None, last_health_status=None)
        outcomes = list(record["outcomes"])
        successes = sum(1 for ok in outcomes if ok)
        failures = len(outcomes) - successes
        if not outcomes:
            verdict = "unknown"
        elif not outcomes[-1]:
            verdict = "failing"
        elif failures:
            verdict = "degraded"
        else:
            verdict = "ok"
        return dict(common, status=verdict, attempts=len(outcomes), successes=successes, failures=failures,
                    success_rate=round(successes / len(outcomes), 3) if outcomes else None,
                    searches=record["searches"], downloads=record["downloads"], refreshes=record["refreshes"],
                    last_search=record["last_search"], last_download=record["last_download"],
                    last_refresh=record["last_refresh"], last_count=record["last_count"],
                    last_error=record["last_error"], last_error_stage=record["last_error_stage"],
                    last_health=record["last_health"], last_health_status=record["last_health_status"])


def _redact_urls(value):
    if isinstance(value, str) and (value.startswith("http://") or value.startswith("https://")):
        parts = urlsplit(value); return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    if isinstance(value, dict): return {k: _redact_urls(v) for k, v in value.items()}
    if isinstance(value, list): return [_redact_urls(v) for v in value]
    return value
