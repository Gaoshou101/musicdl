import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any, Protocol


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
    candidates: dict[int, str]


@dataclass(frozen=True)
class JobResult:
    job_id: str
    duplicate: bool = False


MESSAGE_SCRIPT = """local prior=redis.call('GET',KEYS[2]); if prior then return {1,prior} end; local id=redis.call('XADD',KEYS[1],'*','payload',ARGV[1]); redis.call('SET',KEYS[2],id,'EX',ARGV[2]); return {0,id}"""
ISSUE_SCRIPT = """return redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[2],'NX') and 1 or 0"""
CONSUME_SCRIPT = """local existing=redis.call('GET',KEYS[2]); if existing then local e=cjson.decode(existing); if e.corp_id==ARGV[1] and e.from_user==ARGV[2] and e.request_id==ARGV[3] and e.version==ARGV[4] and e.index==ARGV[5] then return {1,e.job_id} else return {2,''} end end; local p=redis.call('GET',KEYS[1]); if not p then return {2,''} end; local d=cjson.decode(p); if d.corp_id~=ARGV[1] or d.from_user~=ARGV[2] or d.request_id~=ARGV[3] or d.version~=ARGV[4] or not d.candidates[ARGV[5]] then return {2,''} end; local job=redis.call('XADD',KEYS[3],'*','candidate_id',d.candidates[ARGV[5]],'index',ARGV[5],'request_id',ARGV[3]); redis.call('SET',KEYS[2],cjson.encode({corp_id=ARGV[1],from_user=ARGV[2],request_id=ARGV[3],version=ARGV[4],index=ARGV[5],job_id=job}),'EX',ARGV[6]); redis.call('DEL',KEYS[1]); return {0,job}"""


def _string(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class RedisStateStore:
    def __init__(self, client: RedisClient, *, namespace: str = "{musicdl}", message_stream: str = "messages", job_stream: str = "jobs"):
        self.client = client
        self.namespace = namespace
        self.message_stream = f"{namespace}:stream:{message_stream}"
        self.job_stream = f"{namespace}:stream:{job_stream}"

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
        if not context.candidates or any(not isinstance(i, int) or not 1 <= i <= 100 for i in context.candidates) or any(not isinstance(v, str) or not v or len(v) > 512 for v in context.candidates.values()):
            raise ValueError("invalid selection context")
        fields = (context.corp_id, context.from_user, context.request_id, context.candidate_set_version)
        if any(not isinstance(field, str) or not field or len(field) > 256 for field in fields):
            raise ValueError("invalid selection context")
        encoded = json.dumps({"corp_id": context.corp_id, "from_user": context.from_user, "request_id": context.request_id, "version": context.candidate_set_version, "candidates": {str(k): v for k, v in context.candidates.items()}}, separators=(",", ":"))
        for _ in range(max_attempts):
            token = secrets.token_urlsafe(24)
            digest = hashlib.sha256(token.encode()).hexdigest()
            key = f"{self.namespace}:selection:{digest}"
            if _string(await self._eval(ISSUE_SCRIPT, [key], encoded, ttl)) == "1":
                return token
        raise StateUnavailable()

    async def consume_selection(self, token: str, context: SelectionContext, index: int, ttl: int = 86400) -> JobResult:
        if not isinstance(index, int) or not 1 <= index <= 100:
            raise SelectionRejected()
        digest = hashlib.sha256(token.encode()).hexdigest()
        keys = [f"{self.namespace}:selection:{digest}", f"{self.namespace}:selection-job:{digest}", self.job_stream]
        result = await self._eval(CONSUME_SCRIPT, keys, context.corp_id, context.from_user, context.request_id, context.candidate_set_version, str(index), ttl)
        if not isinstance(result, (list, tuple)) or len(result) != 2 or _string(result[0]) not in {"0", "1", "2"}:
            raise StateUnavailable()
        if _string(result[0]) == "2":
            raise SelectionRejected()
        return JobResult(_string(result[1]), _string(result[0]) == "1")
