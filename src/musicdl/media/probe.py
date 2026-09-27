"""Bounded, optional metadata probes using the download transport's egress policy.

Every value this module returns is for display only.  A probe never writes a
size back into a `ResolvedMedia`, so it cannot become the size contract the
download transport enforces; that check keeps reading the resolve answer and the
response headers.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from urllib.parse import urljoin

from musicdl.plugins.broker import coerce_egress_policy
from musicdl.plugins.source import PluginSource
from musicdl.media.transport import MediaTransportError

ITEM_TIMEOUT = 5.0
REQUEST_TIMEOUT = 10.0
MAX_PENDING = 40

UNKNOWN = {"size": None, "extension": None, "media_type": None, "quality": None}


async def probe_media(transport, media, *, policy, timeout=5.0):
    deadline = transport.clock() + timeout
    egress = coerce_egress_policy(policy)

    async def headers(method):
        url = media.url
        for redirects in range(transport.max_redirects + 1):
            hop = await transport._fetch_hop(url, egress, deadline, method=method, range_probe=method == "GET")
            try:
                if 300 <= hop.status < 400:
                    location = hop.headers.get("location")
                    if not location or redirects == transport.max_redirects:
                        raise MediaTransportError("media_redirect_denied")
                    url = urljoin(url, location)
                else:
                    return hop
            finally:
                await transport._discard_handles((hop.response, hop.wrapped, hop.raw))
        raise MediaTransportError("media_redirect_denied")

    hop = await headers("HEAD")
    if hop.status in {403, 405, 501} or (200 <= hop.status < 300 and hop.content_length is None):
        hop = await headers("GET")
        if not 200 <= hop.status < 300:
            return dict(UNKNOWN)
        match = re.fullmatch(r"bytes 0-0/([1-9][0-9]*)", hop.headers.get("content-range", ""))
        size = int(match[1]) if match and len(match[1]) <= 15 else None
        if hop.status == 200:
            size = hop.content_length
    elif 200 <= hop.status < 300:
        size = hop.content_length
    else:
        return dict(UNKNOWN)
    return {"size": size, "extension": media.extension,
            "media_type": hop.content_type or media.media_type, "quality": media.quality}


class ProbeCache:
    """A router-local cache and admission limit shared by concurrent requests."""
    def __init__(self, *, capacity=256, ttl=60.0, clock=time.monotonic):
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self.cache = OrderedDict()
        self.slots = asyncio.Semaphore(4)
        self.tasks = set()

    async def one(self, candidate, source):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ITEM_TIMEOUT
        if not isinstance(source, PluginSource) or source.transport is None or not source.stored.enabled:
            return dict(UNKNOWN)
        manifest = source.stored.manifest
        if candidate.source_id != manifest.plugin_id or candidate.source_version != manifest.version:
            return dict(UNKNOWN)
        context = [candidate.public_representation, manifest.model_dump(mode="json"),
                   id(source), id(source.transport), source.resolve_stream_timeout_ms]
        key = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        cached = self.cache.get(key)
        if cached is not None and cached[0] > self.clock():
            self.cache.move_to_end(key)
            return dict(cached[1])
        try:
            async with asyncio.timeout_at(deadline):
                async with self.slots:
                    remaining_ms = int((deadline - loop.time()) * 1000)
                    if remaining_ms <= 0:
                        return dict(UNKNOWN)
                    media = await source.client.resolve(source.stored, candidate, timeout_ms=remaining_ms)
                    if media.candidate_id != candidate.item_id:
                        return dict(UNKNOWN)
                    if media.declared_size is not None:
                        # Resolve already carried the source's own size, so the
                        # CDN is not touched at all: probing is the fallback for
                        # answers that state no size, never the default path.
                        # This number is a display value like every other probe
                        # result -- it is not written back into a descriptor and
                        # the download transport still judges the stream against
                        # its own headers.
                        result = {"size": media.declared_size, "extension": media.extension,
                                  "media_type": media.media_type, "quality": media.quality}
                    else:
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            return dict(UNKNOWN)
                        result = await probe_media(source.transport, media, policy=manifest.egress,
                                                   timeout=remaining)
        except Exception:
            return dict(UNKNOWN)
        self.cache[key] = (self.clock() + self.ttl, dict(result))
        self.cache.move_to_end(key)
        while len(self.cache) > self.capacity:
            self.cache.popitem(last=False)
        return result

    def finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()


async def probe_candidates(candidates, resolvers, *, cache=None):
    """Return unknowns on failure; cancellation cleanup keeps its admission slot.

    Waiting is bounded independently of transport cleanup, which can outlive
    socket deadlines while a system DNS/connect call finishes in a thread.
    Outstanding work across concurrent requests is capped at forty tasks.
    """
    state = cache if cache is not None else ProbeCache()
    results = {}
    for candidate in candidates:
        results.setdefault(candidate.source_id, {})[candidate.item_id] = dict(UNKNOWN)
    tasks = {}
    for candidate in candidates[:10]:
        if len(state.tasks) >= MAX_PENDING:
            break
        task = asyncio.create_task(state.one(candidate, resolvers.get(candidate.source_id)))
        state.tasks.add(task)
        task.add_done_callback(state.finished)
        tasks[task] = (candidate.source_id, candidate.item_id)
    if not tasks:
        return results
    try:
        done, pending = await asyncio.wait(tasks, timeout=REQUEST_TIMEOUT)
        for task in done:
            if not task.cancelled() and task.exception() is None:
                source_id, item_id = tasks[task]
                results[source_id][item_id] = task.result()
        return results
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
