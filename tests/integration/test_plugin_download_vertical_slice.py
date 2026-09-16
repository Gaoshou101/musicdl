"""In-process vertical slice: plugin search -> selection -> resolve -> main-process archive.

This is deliberately separate from the Docker-only ``test_plugin_runtime.py``. Every process
boundary is faked at its own edge - the plugin runner over in-process HTTP, DNS/connect/TLS in
the media transport, and Redis below the real state store - so the production client, transport,
fallback, artifact ledger, and worker effect machine all run for real without network access.
"""

import asyncio
import hashlib
import json
import socket
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import httpx
import pytest

from musicdl.contracts.plugin import (PluginError, PluginInvocation, PluginManifest, PluginResponse,
                                      PluginStep)
from musicdl.media.transport import SecureMediaTransport
from musicdl.plugins.client import PluginClient
from musicdl.plugins.source import PluginSource
from musicdl.plugins.store import StoredPlugin
from musicdl.sources.registry import SourceEntry, SourceRegistry
from musicdl.sources.search import search_sources
from musicdl.wecom.state import (ARTIFACT_TRANSITION_SCRIPT, CLAIM_ARTIFACT_SCRIPT, CONSUME_SCRIPT,
                                EFFECT_BEGIN_SCRIPT, EFFECT_COMPLETE_SCRIPT, EFFECT_EXTERNAL_SCRIPT,
                                EFFECT_RENEW_SCRIPT, EFFECT_UNCERTAIN_SCRIPT, ISSUE_SCRIPT,
                                MESSAGE_SCRIPT, RedisStateStore)
from musicdl.worker.selection import get_selection_for_request
from musicdl.worker.workers import JobWorker, MessageWorker

PROTOCOL = "musicdl.plugin/v1"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plugins"
ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00"
MEDIA_BYTES = ID3 + b"xyz"
DECLARED_SIZE = len(MEDIA_BYTES)
MEDIA_HOST = "media.example"
MEDIA_URL = f"https://{MEDIA_HOST}/song.mp3"
ALLOWED_HOSTS = (MEDIA_HOST,)
RESOLVED_ADDRESS = "93.184.216.34"
CORP_ID, FROM_USER, REQUEST_ID, QUERY = "corp-vertical", "user-vertical", "req-vertical", "Song"
# The reserved path is fixed by the deterministic classifier, not by the caller.
EXPECTED_RELATIVE = Path("欧美") / "Artist" / "Song - Artist.mp3"


class FakeSocket:
    def __init__(self, raw: bytes):
        self.raw, self.sent, self.closed, self.timeouts = raw, b"", False, []

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, data):
        self.sent += data

    def makefile(self, *args, **kwargs):
        return BytesIO(self.raw)

    def close(self):
        self.closed = True


def media_transport(*, status="200 OK", body=MEDIA_BYTES, content_type="audio/mpeg"):
    """Fake DNS/connect/TLS boundaries; the real transport policy and streaming loop run."""
    head = (f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode()
    sock = FakeSocket(head + body)
    seen = {}

    def resolve(host, port):
        seen["resolve"] = (host, port)
        return [(socket.AF_INET, (RESOLVED_ADDRESS, 443))]

    def connect(address, timeout):
        seen["connect"] = (address, timeout)
        return sock

    return SecureMediaTransport(resolver=resolve, connector=connect,
                                tls_wrap=lambda wrapped, hostname: wrapped), sock, seen


class FixtureRunner:
    """HTTP runner double that executes the fixture ``handle`` for one invocation."""

    def __init__(self, *, fail_resolve=()):
        self.fail_resolve = set(fail_resolve)
        self.requests = []
        self.responses = []
        self.calls = []

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request):
        invocation = PluginInvocation.model_validate_json(request.content)
        self.requests.append(invocation.model_dump(mode="json"))
        plugin_id, operation = invocation.manifest.plugin_id, invocation.request.operation
        self.calls.append((plugin_id, operation))
        if operation == "resolve" and plugin_id in self.fail_resolve:
            step = PluginStep(response=PluginResponse(
                protocol=PROTOCOL, request_id=invocation.request.request_id, operation="resolve",
                ok=False, error=PluginError(code="resolve_failed", message="resolve failed")))
        else:
            step = self.execute(invocation)
        body = step.model_dump(mode="json")
        self.responses.append(body)
        return httpx.Response(200, json=body)

    @staticmethod
    def execute(invocation):
        namespace = {}
        exec(compile(invocation.source, "<fixture>", "exec"), namespace, namespace)
        value = namespace["handle"](invocation.request.model_dump(mode="json"))
        return PluginStep(response=PluginResponse(
            protocol=PROTOCOL, request_id=invocation.request.request_id,
            operation=invocation.request.operation, ok=True, result=value))


class SliceRedis:
    """In-memory Redis double for exactly the commands and scripts this slice exercises.

    ``eval`` dispatches on the script text and mirrors the documented Lua transitions, including
    the ``TIME`` read, so the real Python wrappers run end to end. An unexpected script raises
    instead of silently succeeding, so a new production script cannot slip through these tests.
    """

    NOW_MS = 1_700_000_000_123

    def __init__(self):
        self.strings = {}
        self.hashes = {}
        self.streams = {}
        self.counters = {}
        self.pending = {}
        self.groups = set()
        self.acks = []
        self.dead_letters = []
        self.expiries = []
        self.deleted = []
        self.now = self.NOW_MS

    # --- plain commands ---------------------------------------------------
    async def ping(self): return True
    async def get(self, key): return self.strings.get(str(key))
    async def set(self, key, value, **kwargs): self.strings[str(key)] = value; return True

    async def delete(self, *keys):
        for key in keys:
            self.strings.pop(str(key), None)
            self.deleted.append(str(key))
        return len(keys)

    async def expire(self, key, seconds):
        self.expiries.append((str(key), int(seconds)))
        return True

    async def hgetall(self, key): return dict(self.hashes.get(str(key), {}))

    async def hincrby(self, key, field, amount):
        bucket = self.hashes.setdefault(str(key), {})
        bucket[field] = str(int(bucket.get(field, 0)) + int(amount))
        return int(bucket[field])

    # --- streams ----------------------------------------------------------
    def _next_id(self, stream):
        self.counters[stream] = self.counters.get(stream, 0) + 1
        return f"{self.counters[stream]}-0"

    @staticmethod
    def _fields(fields):
        return {str(key): (value.decode() if isinstance(value, bytes) else str(value))
                for key, value in fields.items()}

    async def xgroup_create(self, stream, group, id="0", mkstream=False):
        self.groups.add((stream, group))
        return True

    async def xadd(self, stream, fields, **kwargs):
        stream = str(stream)
        message = (self._next_id(stream), self._fields(fields))
        self.streams.setdefault(stream, []).append(message)
        return message[0]

    async def xreadgroup(self, group, consumer, streams, count=10, block=None):
        stream = next(iter(streams))
        available = self.streams.setdefault(stream, [])
        delivered = available[:count]
        del available[:len(delivered)]
        if not delivered:
            return []
        self.pending.setdefault((stream, group), []).extend(delivered)
        return [(stream, delivered)]

    async def xautoclaim(self, stream, group, consumer, min_idle_time, start, count=10, **kwargs):
        claimed = self.pending.get((stream, group), [])
        self.pending[(stream, group)] = []
        return ("0-0", claimed[:count])

    async def xack(self, stream, group, message_id):
        self.acks.append((stream, group, message_id))
        pending = self.pending.get((stream, group), [])
        self.pending[(stream, group)] = [item for item in pending if item[0] != message_id]
        return 1

    # --- scripts ----------------------------------------------------------
    async def eval(self, script, numkeys, *args):
        keys = [str(key) for key in args[:numkeys]]
        argv = [str(value) for value in args[numkeys:]]
        if script == MESSAGE_SCRIPT:
            return self._enqueue(keys, argv)
        if script == ISSUE_SCRIPT:
            return self._issue(keys, argv)
        if script == CONSUME_SCRIPT:
            return self._consume(keys, argv)
        if script == CLAIM_ARTIFACT_SCRIPT:
            return self._claim(keys, argv)
        if script == ARTIFACT_TRANSITION_SCRIPT:
            return self._transition(keys, argv)
        if script == EFFECT_BEGIN_SCRIPT:
            return self._effect_begin(keys, argv)
        if script == EFFECT_RENEW_SCRIPT:
            return self._effect_renew(keys, argv)
        if script == EFFECT_EXTERNAL_SCRIPT:
            return self._effect_external(keys, argv)
        if script == EFFECT_COMPLETE_SCRIPT:
            return self._effect_finish(keys, argv, "done")
        if script == EFFECT_UNCERTAIN_SCRIPT:
            return self._effect_finish(keys, argv, "uncertain")
        if "local token = ARGV[2]" in script:
            return self._bind(keys, argv)
        if "TIME" in script:
            return ["1700000000", "123000"]
        raise AssertionError(f"unexpected script: {script[:64]}")

    def _enqueue(self, keys, argv):
        payload, ttl = argv[0], int(argv[1])
        prior = self.strings.get(keys[1])
        if prior:
            return ["1", prior]
        message_id = self._next_id(keys[0])
        self.streams.setdefault(keys[0], []).append((message_id, {"payload": payload}))
        self.strings[keys[1]] = message_id
        self.expiries.append((keys[1], ttl))
        return ["0", message_id]

    def _issue(self, keys, argv):
        encoded, ttl = argv[0], int(argv[1])
        if keys[0] in self.strings:
            return 0
        self.strings[keys[0]] = encoded
        self.expiries.append((keys[0], ttl))
        return 1

    def _bind(self, keys, argv):
        encoded, token, ttl = argv[0], argv[1], int(argv[2])
        self.strings[keys[0]] = encoded
        self.strings[keys[1]] = token
        self.strings[keys[2]] = encoded
        for key in keys:
            self.expiries.append((key, ttl))
        return 1

    def _consume(self, keys, argv):
        selection_key, job_key, stream, record_prefix = keys[:4]
        corp_id, from_user, request_id, version, generation, index, query, ttl = argv
        existing = self.strings.get(job_key)
        if existing:
            record = json.loads(existing)
            same = (record["corp_id"] == corp_id and record["from_user"] == from_user
                    and record["request_id"] == request_id and record["version"] == version
                    and str(record["generation"]) == generation and str(record["index"]) == index)
            return ["1", record["job_id"]] if same else ["2", ""]
        raw = self.strings.get(selection_key)
        if not raw:
            return ["2", ""]
        data = json.loads(raw)
        if (data["corp_id"] != corp_id or data["from_user"] != from_user
                or data["request_id"] != request_id or data["version"] != version
                or str(data["generation"]) != generation):
            return ["2", ""]
        candidate = (data.get("candidates") or {}).get(index)
        if candidate is None:
            return ["2", ""]
        frozen = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        message_id = self._next_id(stream)
        self.streams.setdefault(stream, []).append((message_id, {
            "candidate": frozen, "corp_id": corp_id, "from_user": from_user, "query": query,
            "request_id": request_id, "version": version, "generation": generation, "index": index}))
        record = json.dumps({"corp_id": corp_id, "from_user": from_user, "request_id": request_id,
                             "version": version, "generation": int(generation), "index": index,
                             "job_id": message_id, "candidate": candidate},
                            ensure_ascii=False, separators=(",", ":"))
        self.strings[record_prefix + message_id] = record
        self.strings[job_key] = record
        self.expiries.append((record_prefix + message_id, int(ttl)))
        self.expiries.append((job_key, int(ttl)))
        del self.strings[selection_key]
        return ["0", message_id]

    @staticmethod
    def _flat(values):
        result = []
        for key, value in values.items():
            result.extend([key, value])
        return result

    def _claim(self, keys, argv):
        slot_key, record_key = keys
        existing = self.hashes.get(record_key)
        if existing:
            return self._flat(existing)
        bucket = self.hashes.setdefault(slot_key, {})
        if argv[0] in bucket:
            return ["__conflict__"]
        bucket[argv[0]] = argv[1]
        created = {"job_id": argv[1], "candidate_id": argv[2], "base_hash": argv[3],
                   "target_relative_path": argv[4], "temporary_relative_path": argv[5],
                   "extension": argv[6], "media_type": argv[7], "declared_size": argv[8],
                   "owner": argv[9], "fence": argv[10], "allocation_slot": argv[0],
                   "state": "prepared"}
        self.hashes[record_key] = created
        if int(argv[11]) > 0:
            self.expiries.append((record_key, int(argv[11])))
        return self._flat(created)

    def _transition(self, keys, argv):
        values = self.hashes.get(keys[0])
        if not values:
            return ["__missing__"]
        (owner, fence, allowed, state, ttl, size_bytes, sha256, extension, media_type,
         declared_size, new_owner, new_fence, lease_until) = argv
        if owner and values.get("owner") != owner:
            return ["__stale__"]
        if fence and str(values.get("fence")) != fence:
            return ["__stale__"]
        if values.get("state") not in set(allowed.split(",")):
            return ["__stale__"]
        if state:
            values["state"] = state
        for name, value in (("size_bytes", size_bytes), ("sha256", sha256),
                            ("extension", extension), ("media_type", media_type),
                            ("declared_size", declared_size), ("owner", new_owner),
                            ("fence", new_fence), ("lease_until_ms", lease_until)):
            if value:
                values[name] = value
        if int(ttl) > 0:
            self.expiries.append((keys[0], int(ttl)))
        return self._flat(values)

    @staticmethod
    def _effect_lease(job_id, effect, owner, fence, until_ms, stage):
        return ["lease", job_id, effect, owner, str(fence), str(until_ms), stage]

    @staticmethod
    def _effect_record(values, status):
        return ["record", values["job_id"], values["effect"], status, values["stage"],
                values.get("owner", ""), values.get("fence", "0"),
                values.get("lease_until_ms", ""), values.get("result", "")]

    def _effect_begin(self, keys, argv):
        job_id, effect, owner, lease_ms, ttl = argv
        values = self.hashes.get(keys[0])
        if not values:
            values = {"job_id": job_id, "effect": effect, "owner": owner, "fence": "1",
                      "stage": "claimed", "status": "running",
                      "lease_until_ms": str(self.now + int(lease_ms))}
            self.hashes[keys[0]] = values
            if int(ttl) > 0:
                self.expiries.append((keys[0], int(ttl)))
            return self._effect_lease(job_id, effect, owner, 1, values["lease_until_ms"], "claimed")
        if values["status"] in {"done", "uncertain"}:
            return self._effect_record(values, values["status"])
        held = int(values.get("lease_until_ms") or 0)
        if held > self.now:
            if values["owner"] == owner:
                return self._effect_lease(job_id, effect, owner, values["fence"], held, values["stage"])
            return self._effect_record(values, "busy")
        if values["stage"] == "external_started":
            values.update({"status": "uncertain", "stage": "uncertain", "lease_until_ms": ""})
            if int(ttl) > 0:
                self.expiries.append((keys[0], int(ttl)))
            return self._effect_record(values, "uncertain")
        values.update({"owner": owner, "fence": str(int(values.get("fence", "0")) + 1),
                       "stage": "claimed", "status": "running",
                       "lease_until_ms": str(self.now + int(lease_ms))})
        values.pop("result", None)
        if int(ttl) > 0:
            self.expiries.append((keys[0], int(ttl)))
        return self._effect_lease(job_id, effect, owner, values["fence"], values["lease_until_ms"], "claimed")

    def _effect_renew(self, keys, argv):
        values = self.hashes.get(keys[0])
        if not values:
            return ["__missing__"]
        job_id, effect, owner, fence, lease_ms = argv
        if values["owner"] != owner or str(values["fence"]) != fence or values["status"] != "running":
            return ["__stale__"]
        values["lease_until_ms"] = str(self.now + int(lease_ms))
        return self._effect_lease(job_id, effect, owner, fence, values["lease_until_ms"], values["stage"])

    def _effect_external(self, keys, argv):
        values = self.hashes.get(keys[0])
        if not values:
            return ["__missing__"]
        job_id, effect, owner, fence = argv[:4]
        if values["owner"] != owner or str(values["fence"]) != fence:
            return ["__stale__"]
        if values["status"] in {"done", "uncertain"}:
            return self._effect_record(values, values["status"])
        if values["status"] != "running":
            return ["__stale__"]
        values["stage"] = "external_started"
        return self._effect_lease(job_id, effect, owner, fence,
                                  values.get("lease_until_ms", ""), "external_started")

    def _effect_finish(self, keys, argv, status):
        values = self.hashes.get(keys[0])
        if not values:
            return ["__missing__"]
        job_id, effect, owner, fence = argv[:4]
        if values["owner"] != owner or str(values["fence"]) != fence or values["status"] != "running":
            return ["__stale__"]
        values["status"] = status
        values["stage"] = "completed" if status == "done" else "uncertain"
        values["result"] = argv[4]
        values["lease_until_ms"] = ""
        return self._effect_record(values, status)


class RecordingWeCom:
    def __init__(self):
        self.sent = []

    async def send_text(self, user, content):
        self.sent.append((user, content))
        return {}


@dataclass(frozen=True)
class Slice:
    redis: SliceRedis
    state: RedisStateStore
    client: PluginClient
    http: httpx.AsyncClient
    message_worker: MessageWorker
    job_worker: JobWorker
    wecom: RecordingWeCom
    refresh_calls: list
    media_root: Path


def fixture_plugin(name, operations=("search", "resolve", "health")):
    path = FIXTURES / f"vertical_{name}.py"
    source = path.read_bytes()
    manifest = PluginManifest(plugin_id=name, version="1", language="python",
                              operations=operations, allowed_hosts=ALLOWED_HOSTS,
                              sha256=hashlib.sha256(source).hexdigest())
    return StoredPlugin(manifest, path, True)


def build_slice(tmp_path, *, runner, transport, names=("primary", "backup")):
    """Wire the real client, adapter, transport, ledger, and workers over the fake boundaries."""
    redis = SliceRedis()
    state = RedisStateStore(redis)
    http = httpx.AsyncClient(transport=runner.transport(), trust_env=False, follow_redirects=False)
    client = PluginClient("http://runner:8080", http_client=http, timeout=5)
    entries, source_map = [], {}
    for index, name in enumerate(names):
        source = PluginSource(fixture_plugin(name), client, transport,
                              resolve_stream_timeout_ms=15_000, health_timeout_ms=5_000)
        # Priority keeps the primary source in front of the cross-source duplicate candidate.
        entries.append(SourceEntry(name, "1", source, priority=index))
        source_map[name] = source
    registry = SourceRegistry(entries)
    refresh_calls = []

    async def refresh(query, excluded):
        refresh_calls.append((query, excluded))
        remaining = SourceRegistry(entry for entry in entries if entry.source_id not in excluded)
        return await search_sources(remaining, query, timeout=5.0)

    wecom = RecordingWeCom()
    media_root = tmp_path / "media"
    message_worker = MessageWorker(redis, registry, wecom, state=state, search_timeout=5.0,
                                   selection_ttl=600)
    job_worker = JobWorker(redis, wecom, sources=source_map, media_root=str(media_root), state=state,
                           refresh=refresh, job_timeout=30.0, resolve_stream_timeout=15.0,
                           refresh_timeout=5.0, health_timeout=5.0, pending_idle_ms=32000,
                           job_ttl=172800, retry_window_seconds=86400, max_attempts=3,
                           selection_ttl=600)
    return Slice(redis, state, client, http, message_worker, job_worker, wecom, refresh_calls,
                 media_root)


async def search_once(slice_):
    """Publish one WeCom search message and run exactly one worker pass over it."""
    envelope = {"corp_id": CORP_ID, "from_user": FROM_USER, "request_id": REQUEST_ID,
                "payload": {"command": "search", "value": QUERY, "msg_type": "text"}}
    await slice_.redis.xadd(slice_.state.message_stream, {"payload": json.dumps(envelope)})
    assert await slice_.message_worker.run_once() == 1
    return await get_selection_for_request(slice_.redis, REQUEST_ID)


async def select_and_run(slice_, bound, index=1):
    """Consume one selection token and run exactly one job pass over the enqueued job."""
    job = await slice_.job_worker.handle_user_selection(bound["token"], index)
    assert await slice_.job_worker.run_once() == 1
    return job


def assert_runner_bodies_are_metadata_only(runner):
    """Runner bodies carry descriptors and metadata only: never media bytes or credentials."""
    for payload in (*runner.requests, *runner.responses):
        blob = json.dumps(payload)
        assert "ID3" not in blob
        lowered = blob.casefold()
        for forbidden in ("base64", "password", "authorization", "api_key", "bearer "):
            assert forbidden not in lowered


def test_vertical_slice_searches_resolves_streams_and_archives(tmp_path):
    async def scenario():
        runner = FixtureRunner()
        transport, sock, seen = media_transport()
        slice_ = build_slice(tmp_path, runner=runner, transport=transport)
        try:
            bound = await search_once(slice_)
            job = await select_and_run(slice_, bound)
            return (slice_, runner, sock, seen, bound, job,
                    await slice_.state.get_artifact(job.job_id),
                    await slice_.state.get_job_effect(job.job_id, "download"))
        finally:
            await slice_.http.aclose()

    slice_, runner, sock, seen, bound, job, artifact, effect = asyncio.run(scenario())

    # The search returned the deduplicated primary candidate and nothing was downloaded yet.
    assert bound["candidates"]["1"]["source_id"] == "primary"
    # Both sources are searched concurrently, so the two searches may complete in either
    # order; only the resolve is ordered after them because it belongs to the later job pass.
    assert Counter(runner.calls) == Counter({("primary", "search"): 1, ("backup", "search"): 1,
                                             ("primary", "resolve"): 1})
    assert runner.calls[-1] == ("primary", "resolve")
    resolves = [item["request"] for item in runner.requests
                if item["request"]["operation"] == "resolve"]
    assert len(resolves) == 1
    assert resolves[0]["payload"] == {"candidate": bound["candidates"]["1"]}

    # The main process owned DNS, connect, TLS SNI, and the fixed request line.
    assert seen["resolve"] == (MEDIA_HOST, 443)
    assert seen["connect"][0] == (RESOLVED_ADDRESS, 443)
    assert b"GET /song.mp3 HTTP/1.1\r\n" in sock.sent
    assert f"Host: {MEDIA_HOST}\r\n".encode() in sock.sent
    assert sock.closed

    # Phase 4 published the exact bytes once and replayed nothing.
    published = slice_.media_root / EXPECTED_RELATIVE
    assert published.read_bytes() == MEDIA_BYTES
    assert artifact.state == "published"
    assert (artifact.size_bytes, artifact.target_relative_path) == (
        DECLARED_SIZE, EXPECTED_RELATIVE.as_posix())
    assert artifact.sha256 == hashlib.sha256(MEDIA_BYTES).hexdigest()
    assert effect.status == "done" and effect.result == {"ok": True}
    assert not list(slice_.media_root.rglob("*.part"))

    # WeCom saw the selection prompt and then the recorded success notice.
    assert slice_.wecom.sent[0][0] == FROM_USER
    assert "primary@1" in slice_.wecom.sent[0][1] and "回复序号下载。" in slice_.wecom.sent[0][1]
    assert slice_.wecom.sent[1][0] == FROM_USER
    assert slice_.wecom.sent[1][1].startswith("下载成功：")
    assert Path(slice_.wecom.sent[1][1].removeprefix("下载成功：")) == EXPECTED_RELATIVE

    # Both stream messages were acknowledged only after their effects were recorded.
    assert slice_.redis.acks == [(slice_.state.message_stream, slice_.message_worker.group, "1-0"),
                                 (slice_.state.job_stream, slice_.job_worker.group, job.job_id)]
    assert slice_.redis.dead_letters == []
    assert_runner_bodies_are_metadata_only(runner)


@pytest.mark.parametrize("mode,expected_error", [
    ("resolve", "download_failed"),
    ("stream", "media_response_invalid"),
])
def test_vertical_slice_failure_reprompts_without_downloading_a_replacement(tmp_path, mode, expected_error):
    async def scenario():
        runner = FixtureRunner(fail_resolve={"primary"} if mode == "resolve" else ())
        transport, _sock, _seen = (media_transport(status="404 Not Found", body=b"denied")
                                   if mode == "stream" else media_transport())
        slice_ = build_slice(tmp_path, runner=runner, transport=transport)
        try:
            bound = await search_once(slice_)
            job = await select_and_run(slice_, bound)
            return (slice_, runner, bound, job,
                    await slice_.state.get_artifact(job.job_id),
                    await slice_.state.get_job_effect(job.job_id, "download"),
                    await get_selection_for_request(slice_.redis, REQUEST_ID))
        finally:
            await slice_.http.aclose()

    slice_, runner, bound, job, artifact, effect, rebound = asyncio.run(scenario())

    # Exactly one failed resolve, one excluded-source refresh, and one health check, and no
    # operation ever asked a plugin for media bytes.
    assert Counter(runner.calls) == Counter({("primary", "search"): 1, ("backup", "search"): 2,
                                             ("primary", "resolve"): 1, ("primary", "health"): 1})
    assert slice_.refresh_calls == [(QUERY, frozenset({"primary"}))]
    assert effect.status == "done" and effect.result == {"ok": False, "code": expected_error}

    # The refresh replaced the failed source under a new generation and version, and the
    # replacement was never resolved, streamed, or published.
    assert rebound["generation"] == 1 and rebound["version"] != bound["version"]
    assert rebound["candidates"]["1"]["source_id"] == "backup"
    assert rebound["candidates"]["1"]["item_id"] == "backup-1"
    assert artifact.state == "prepared"
    assert not list(slice_.media_root.rglob("*.mp3"))
    assert not list(slice_.media_root.rglob("*.part"))

    # WeCom saw the original prompt and exactly one replacement prompt, never a success notice.
    assert [user for user, _ in slice_.wecom.sent] == [FROM_USER, FROM_USER]
    assert "primary@1" in slice_.wecom.sent[0][1]
    assert "backup@1" in slice_.wecom.sent[1][1] and "回复序号下载。" in slice_.wecom.sent[1][1]
    assert all("下载成功" not in text for _, text in slice_.wecom.sent)

    # The job message was acknowledged once its fenced effects settled; nothing was dead-lettered.
    assert len(slice_.redis.acks) == 2
    assert (slice_.state.message_stream, slice_.message_worker.group, "1-0") in slice_.redis.acks
    assert (slice_.state.job_stream, slice_.job_worker.group, job.job_id) in slice_.redis.acks
    assert slice_.redis.dead_letters == []
    assert_runner_bodies_are_metadata_only(runner)
