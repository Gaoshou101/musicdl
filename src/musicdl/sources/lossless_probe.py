"""The per-source lossless self-check: one resolve, one verdict, one cache.

A channel either hands over a lossless file for a song it lists or it does not,
and only a real resolve says which.  What this process measured becomes a
preference for the search that picks a channel, never a claim written onto a
candidate: the answer is evidence, not metadata.  So the check asks one
question, judges it from the fields the source's own answer carries -- it never
parses a URL itself -- and lets every failure -- a timeout, an exception, an
answer with no container -- stay ``unknown`` instead of quietly becoming
"incapable".

Nothing here is on the request path.  A check is coalesced per source id, so
concurrent askers share one in-flight probe, and a finished answer is recorded
once in the health roll-up that the search reads as a tie-break.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Mapping

from musicdl.admin.health import LOSSLESS_TTL

# The one tier a capability check asks a source for.  It is a request, not an
# assertion: the answer decides what was really served.
PROBE_QUALITY = "flac"

# A single check has to finish inside the panel's own patience, so the resolve
# it runs is bounded; a check that runs long is an ``unknown``, not a hang.
PROBE_TIMEOUT = 10.0

# Containers that only ever carry lossless audio, and the lossy ones a source
# can answer with.  ``m4a`` is deliberately in neither: the container alone
# does not say whether the payload is ALAC or AAC, so it is only judged by the
# quality field beside it.
LOSSLESS_CONTAINERS = frozenset({"flac", "alac", "ape", "wav", "aiff"})
LOSSLESS_QUALITIES = LOSSLESS_CONTAINERS | {"flac24bit"}
LOSSY_CONTAINERS = frozenset({"mp3", "ogg"})
LOSSY_QUALITIES = frozenset({"128k", "192k", "320k", "mp3", "aac", "ogg", "opus"})
# The containers that hold a lossless payload only when the label beside them
# names it.  An MP4 container carries ALAC or AAC, so ``alac`` is proof and any
# other lossless label is not.
LABELED_LOSSLESS_CONTAINERS = {"m4a": frozenset({"alac"})}


def _token(value) -> str | None:
    """A lower-cased field value, or ``None`` when there is nothing to read."""
    if not isinstance(value, str):
        return None
    token = value.strip().casefold().lstrip(".")
    return token or None


def _version(value) -> str | None:
    """A source version worth keying a record on; anything else is ``None``."""
    return value if isinstance(value, str) and value else None


def _holds_lossless(extension: str | None, quality: str) -> bool:
    """Whether a container could be carrying the lossless tier beside it.

    Nothing named is nothing to contradict the label.  A lossless container
    holds its own payload by definition, and a container that needs the label
    to say so -- an MP4 container -- holds it only when the label names the one
    lossless payload that container carries.
    """
    if extension is None or extension in LOSSLESS_CONTAINERS:
        return True
    allowed = LABELED_LOSSLESS_CONTAINERS.get(extension)
    return allowed is not None and quality in allowed


def verdict_of(media) -> tuple[str, dict]:
    """Judge one resolved answer from what it declared, container and tier.

    The two fields are read in that order of strength, because only one of them
    can be a finding on its own.  A lossy container is: an mp3 or ogg payload is
    lossy whatever the answer called it, so it decides first and no label talks
    it away.  The other direction is not symmetric -- a lossless container is
    not evidence at all, because the measured no-suffix endpoint hands back the
    container the caller asked for -- so a lossless tier is believed only when
    the container beside it can hold that payload.  The URL itself is never
    consulted, because a path that merely ends in ``.flac`` is a name, not a
    container.  ``evidence`` carries both fields either way, so the operator can
    read back exactly what was judged.
    """
    extension = _token(getattr(media, "extension", None))
    quality = _token(getattr(media, "actual_quality", None)) or _token(getattr(media, "quality", None))
    evidence = {"extension": extension, "quality": quality}
    if extension in LOSSY_CONTAINERS or quality in LOSSY_QUALITIES:
        return "lossy", evidence
    if quality in LOSSLESS_QUALITIES and _holds_lossless(extension, quality):
        return "lossless", evidence
    return "unknown", evidence


class LosslessProbe:
    """Ask one channel whether it can serve lossless, once, and remember it.

    ``resolver`` is an injected async callable given a source id; it returns a
    resolved-media-like object (``extension`` plus ``actual_quality`` or
    ``quality``) or raises.  ``observer`` is either a
    :class:`~musicdl.admin.health.SourceHealthStore` or a plain callback with
    the store's recording signature.  Concurrent checks of one source share a
    single in-flight probe, and a check that fails is recorded as ``unknown``
    -- never as "incapable".
    """

    def __init__(self, resolver, observer, *, ttl: float = LOSSLESS_TTL, clock=None,
                 timeout: float | None = PROBE_TIMEOUT) -> None:
        if not callable(resolver):
            raise ValueError("invalid resolver")
        if observer is None or not (callable(observer) or hasattr(observer, "observe_lossless")):
            raise ValueError("invalid observer")
        self.ttl = _positive(ttl, "invalid ttl")
        self.timeout = None if timeout is None else _positive(timeout, "invalid timeout")
        self._resolver = resolver
        self._observer = observer
        self._clock = clock if callable(clock) else time.time
        self._accepts_quality = _accepts_quality(resolver)
        # One entry per source id, so a burst of askers runs one resolve.
        self._inflight: dict[str, asyncio.Future] = {}

    # -- the check -------------------------------------------------------
    async def check(self, source_id: str, *, source_version: str | None = None) -> dict:
        """Run (or reuse) one capability check and return its verdict."""
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("invalid source id")
        cached = self._cached(source_id, source_version)
        if cached is not None:
            return cached
        pending = self._inflight.get(source_id)
        if pending is None:
            pending = asyncio.ensure_future(self._run(source_id, source_version))
            self._inflight[source_id] = pending
            pending.add_done_callback(lambda _task, key=source_id: self._inflight.pop(key, None))
        # Shielded so one asker giving up does not cancel the shared probe the
        # others are still waiting on.
        return await asyncio.shield(pending)

    async def _run(self, source_id: str, source_version: str | None) -> dict:
        status, evidence = await self._measure(source_id)
        checked_at = self._now()
        self._record(source_id, status=status, evidence=evidence,
                     source_version=source_version, checked_at=checked_at)
        return {"status": status, "evidence": evidence, "checked_at": checked_at}

    async def _measure(self, source_id: str) -> tuple[str, dict]:
        """One bounded resolve, folded into a verdict; every failure is unknown."""
        try:
            media = await self._resolve(source_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return "unknown", {"extension": None, "quality": None}
        return verdict_of(media)

    async def _resolve(self, source_id: str):
        try:
            pending = self._invoke(source_id)
        except TypeError:
            # Only a resolver whose signature could not be read gets the
            # one-argument retry: a TypeError from a readable signature is a
            # real failure, and re-calling could resolve a second time.
            if self._accepts_quality is not None:
                raise
            pending = self._resolver(source_id)
        if self.timeout is None:
            return await pending
        return await asyncio.wait_for(pending, timeout=self.timeout)

    def _invoke(self, source_id: str):
        if self._accepts_quality is False:
            return self._resolver(source_id)
        return self._resolver(source_id, quality=PROBE_QUALITY)

    # -- the cache the check reads and writes ----------------------------
    def _cached(self, source_id: str, source_version: str | None) -> dict | None:
        """A recorded answer that is still fresh and still about this version."""
        status = self._status(source_id)
        if status is None:
            return None
        checked_at = status.get("checked_at")
        if checked_at is None:
            return None
        if status.get("source_version") != _version(source_version):
            return None
        if self._now() - checked_at > self.ttl:
            return None
        return {"status": status["status"], "evidence": dict(status.get("evidence") or {}),
                "checked_at": checked_at}

    def _status(self, source_id: str) -> Mapping | None:
        reader = getattr(self._observer, "lossless_status", None)
        if reader is None:
            return None
        try:
            value = reader(source_id)
        except Exception:
            return None
        return value if isinstance(value, Mapping) else None

    def _record(self, source_id: str, *, status: str, evidence: dict,
                source_version: str | None, checked_at: float) -> None:
        capable = {"lossless": True, "lossy": False}.get(status)
        writer = getattr(self._observer, "observe_lossless", None)
        try:
            if writer is not None:
                writer(source_id, capable=capable, evidence=evidence,
                       source_version=_version(source_version), checked_at=checked_at)
            elif callable(self._observer):
                self._observer(source_id, capable=capable, evidence=evidence,
                               source_version=_version(source_version), checked_at=checked_at)
        except Exception:
            # Recording is bookkeeping: a roll-up that cannot be written must
            # not turn a measured answer into a failed check.
            pass

    def _now(self) -> float:
        return self._clock()


def _positive(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value <= 0:
        raise ValueError(name)
    return float(value)


def _accepts_quality(resolver) -> bool | None:
    """Whether the resolver takes the pinned ``quality`` keyword.

    ``None`` means the signature could not be read, which is the one case where
    the caller may retry the one-argument form.
    """
    try:
        parameters = inspect.signature(resolver).parameters
    except (TypeError, ValueError):
        return None
    if "quality" in parameters:
        return True
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
