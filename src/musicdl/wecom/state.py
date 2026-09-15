import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

from musicdl.sources.models import Candidate


class StateUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("state store unavailable")


class SelectionRejected(ValueError):
    def __init__(self):
        super().__init__("selection rejected")


class RedisClient(Protocol):
    async def eval(self, script: str, numkeys: int, *args: Any) -> Any: ...
    async def get(self, key: str) -> Any: ...
    async def ping(self) -> Any: ...


@dataclass(frozen=True)
class EnqueueResult:
    stream_id: str
    duplicate: bool = False


@dataclass(frozen=True)
class SelectionContext:
    corp_id: str
    from_user: str
    request_id: str
    candidate_set_version: str
    candidates: dict[int, Candidate]
    query: str = ""
    selection_generation: int = 0


@dataclass(frozen=True)
class JobResult:
    job_id: str
    duplicate: bool = False


MESSAGE_SCRIPT = """local prior=redis.call('GET',KEYS[2]); if prior then return {1,prior} end; local id=redis.call('XADD',KEYS[1],'*','payload',ARGV[1]); redis.call('SET',KEYS[2],id,'EX',ARGV[2]); return {0,id}"""
ISSUE_SCRIPT = """return redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[2],'NX') and 1 or 0"""
CONSUME_SCRIPT = """local existing=redis.call('GET',KEYS[2]);
if existing then local e=cjson.decode(existing);
  if e.corp_id==ARGV[1] and e.from_user==ARGV[2] and e.request_id==ARGV[3] and e.version==ARGV[4] and tostring(e.generation)==ARGV[5] and tostring(e.index)==ARGV[6] then return {1,e.job_id} else return {2,''} end
end;
local p=redis.call('GET',KEYS[1]);
if not p then return {2,''} end;
local d=cjson.decode(p);
if d.corp_id~=ARGV[1] or d.from_user~=ARGV[2] or d.request_id~=ARGV[3] or d.version~=ARGV[4] or tostring(d.generation)~=ARGV[5] then return {2,''} end;
local candidate=d.candidates[ARGV[6]];
if not candidate then return {2,''} end;
local frozen=cjson.encode(candidate);
local job=redis.call('XADD',KEYS[3],'*','candidate',frozen,'corp_id',ARGV[1],'from_user',ARGV[2],'query',ARGV[7],'request_id',ARGV[3],'version',ARGV[4],'generation',ARGV[5],'index',ARGV[6]);
local record=cjson.encode({corp_id=ARGV[1],from_user=ARGV[2],request_id=ARGV[3],version=ARGV[4],generation=tonumber(ARGV[5]),index=ARGV[6],job_id=job,candidate=frozen});
redis.call('SET',KEYS[4]..job,record,'EX',ARGV[8]);
redis.call('SET',KEYS[2],record,'EX',ARGV[8]);
redis.call('DEL',KEYS[1]);
return {0,job}"""


def freeze_candidates(candidates: Any) -> dict[str, Any]:
    """Freeze an index -> Candidate mapping into canonical JSON snapshots."""
    if not isinstance(candidates, dict) or not candidates:
        raise ValueError("invalid selection context")
    frozen: dict[str, Any] = {}
    for index, value in candidates.items():
        if isinstance(index, str) and index.isdigit():
            index = int(index)
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= 100:
            raise ValueError("invalid selection context")
        if not isinstance(value, Candidate):
            try:
                value = Candidate.model_validate(value)
            except Exception as exc:
                raise ValueError("invalid selection context") from exc
        frozen[str(index)] = value.model_dump(mode="json")
    return frozen


def selection_payload(context: SelectionContext) -> dict[str, Any]:
    """Validate a selection context and freeze every candidate to canonical JSON."""
    fields = (context.corp_id, context.from_user, context.request_id, context.candidate_set_version)
    if any(not isinstance(field, str) or not field or len(field) > 256 for field in fields):
        raise ValueError("invalid selection context")
    if not isinstance(context.query, str) or len(context.query) > 512:
        raise ValueError("invalid selection context")
    generation = context.selection_generation
    if isinstance(generation, bool) or not isinstance(generation, int) or not 0 <= generation <= 2 ** 31 - 1:
        raise ValueError("invalid selection context")
    frozen = freeze_candidates(context.candidates)
    return {"corp_id": context.corp_id, "from_user": context.from_user, "request_id": context.request_id,
            "version": context.candidate_set_version, "query": context.query,
            "generation": generation, "candidates": frozen}


def _string(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class RedisStateStore:
    def __init__(self, client: RedisClient, *, namespace: str = "{musicdl}", message_stream: str = "messages", job_stream: str = "jobs"):
        self.client = client
        self.namespace = namespace
        self.message_stream = f"{namespace}:stream:{message_stream}"
        self.job_stream = f"{namespace}:stream:{job_stream}"
        self.job_record_prefix = f"{namespace}:job:"

    async def _eval(self, script: str, keys: list[str], *args: Any) -> Any:
        try:
            return await self.client.eval(script, len(keys), *keys, *args)
        except Exception as exc:
            raise StateUnavailable() from exc

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())
        except Exception as exc:
            raise StateUnavailable() from exc

    async def lookup_message(self, corp_id: str, request_id: str) -> bool:
        key = f"{self.namespace}:dedup:message:{corp_id}:{request_id}"
        try:
            return bool(await self.client.get(key))
        except Exception as exc:
            raise StateUnavailable() from exc

    async def enqueue_message(self, corp_id: str, from_user: str, request_id: str, payload: dict[str, Any], ttl: int = 86400) -> EnqueueResult:
        key = f"{self.namespace}:dedup:message:{corp_id}:{request_id}"
        encoded = json.dumps({"corp_id": corp_id, "from_user": from_user, "request_id": request_id, "payload": payload}, ensure_ascii=False, separators=(",", ":"))
        result = await self._eval(MESSAGE_SCRIPT, [self.message_stream, key], encoded, ttl)
        if not isinstance(result, (list, tuple)) or len(result) != 2 or _string(result[0]) not in {"0", "1"}:
            raise StateUnavailable()
        return EnqueueResult(_string(result[1]), _string(result[0]) == "1")

    async def issue_selection(self, context: SelectionContext, ttl: int = 600, max_attempts: int = 5) -> str:
        encoded = json.dumps(selection_payload(context), separators=(",", ":"), ensure_ascii=False)
        for _ in range(max_attempts):
            token = secrets.token_urlsafe(24)
            digest = hashlib.sha256(token.encode()).hexdigest()
            key = f"{self.namespace}:selection:{digest}"
            if _string(await self._eval(ISSUE_SCRIPT, [key], encoded, ttl)) == "1":
                return token
        raise StateUnavailable()

    async def consume_selection(self, token: str, context: SelectionContext, index: int, *, ttl: int = 172800) -> JobResult:
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= 100:
            raise SelectionRejected()
        payload = selection_payload(context)
        if str(index) not in payload["candidates"]:
            raise SelectionRejected()
        digest = hashlib.sha256(token.encode()).hexdigest()
        keys = [f"{self.namespace}:selection:{digest}", f"{self.namespace}:selection-job:{digest}", self.job_stream,
                self.job_record_prefix]
        result = await self._eval(CONSUME_SCRIPT, keys, payload["corp_id"], payload["from_user"], payload["request_id"],
                                  payload["version"], str(payload["generation"]), str(index), payload["query"], ttl)
        if not isinstance(result, (list, tuple)) or len(result) != 2 or _string(result[0]) not in {"0", "1", "2"}:
            raise StateUnavailable()
        if _string(result[0]) == "2":
            raise SelectionRejected()
        return JobResult(_string(result[1]), _string(result[0]) == "1")
