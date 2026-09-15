import hashlib
import json
import secrets
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from musicdl.media.models import ArtifactRecord, ArtifactTakeoverAction
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
    async def hgetall(self, key: str) -> Any: ...
    async def ping(self) -> Any: ...


class ArtifactConflict(RuntimeError):
    """A stale owner, fence, or state prevented an artifact mutation."""

    def __init__(self, code: str = "artifact_conflict"):
        self.code = code
        super().__init__(code)


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


class EffectConflict(RuntimeError):
    """A stale owner, fence, or stage prevented a job-effect mutation."""

    def __init__(self, code: str = "effect_conflict"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class JobEffect:
    job_id: str
    effect: str
    status: str
    stage: str
    owner: str | None
    fence: int
    lease_until_ms: int | None
    result: dict[str, Any] | None = None


@dataclass(frozen=True)
class EffectLease:
    job_id: str
    effect: str
    owner: str
    fence: int
    lease_until_ms: int
    stage: str


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


CLAIM_ARTIFACT_SCRIPT = """local record=redis.call('HGETALL',KEYS[2]);
if #record>0 then return record end;
if redis.call('HSETNX',KEYS[1],ARGV[1],ARGV[2])==0 then return {'__conflict__'} end;
redis.call('HSET',KEYS[2],'job_id',ARGV[2],'candidate_id',ARGV[3],'base_hash',ARGV[4],'target_relative_path',ARGV[5],'temporary_relative_path',ARGV[6],'extension',ARGV[7],'media_type',ARGV[8],'declared_size',ARGV[9],'owner',ARGV[10],'fence',ARGV[11],'allocation_slot',ARGV[1],'state','prepared');
if tonumber(ARGV[12])>0 then redis.call('EXPIRE',KEYS[2],ARGV[12]) end;
return redis.call('HGETALL',KEYS[2])"""
ARTIFACT_TRANSITION_SCRIPT = """local r=redis.call('HGETALL',KEYS[1]);
if #r==0 then return {'__missing__'} end;
local d={};
for i=1,#r,2 do d[r[i]]=r[i+1] end;
if ARGV[1]~='' and d['owner']~=ARGV[1] then return {'__stale__'} end;
if ARGV[2]~='' and tostring(d['fence'])~=ARGV[2] then return {'__stale__'} end;
local allowed={};
for s in string.gmatch(ARGV[3],'[^,]+') do allowed[s]=true end;
if not allowed[d['state']] then return {'__stale__'} end;
if ARGV[4]~='' then redis.call('HSET',KEYS[1],'state',ARGV[4]) end;
if ARGV[6]~='' then redis.call('HSET',KEYS[1],'size_bytes',ARGV[6]) end;
if ARGV[7]~='' then redis.call('HSET',KEYS[1],'sha256',ARGV[7]) end;
if ARGV[8]~='' then redis.call('HSET',KEYS[1],'extension',ARGV[8]) end;
if ARGV[9]~='' then redis.call('HSET',KEYS[1],'media_type',ARGV[9]) end;
if ARGV[10]~='' then redis.call('HSET',KEYS[1],'declared_size',ARGV[10]) end;
if ARGV[11]~='' then redis.call('HSET',KEYS[1],'owner',ARGV[11]) end;
if ARGV[12]~='' then redis.call('HSET',KEYS[1],'fence',ARGV[12]) end;
if ARGV[13]~='' then redis.call('HSET',KEYS[1],'lease_until_ms',ARGV[13]) end;
if tonumber(ARGV[5])>0 then redis.call('EXPIRE',KEYS[1],ARGV[5]) end;
return redis.call('HGETALL',KEYS[1])"""

EFFECT_NAMES = frozenset({
    "download", "refresh", "health", "rebind",
    "success_notice", "selection_prompt", "terminal_failure_notice",
})
EFFECT_RESULT_MAX_BYTES = 4096
EFFECT_RESULT_MAX_FIELDS = 32
EFFECT_RESULT_MAX_STRING = 512

_EFFECT_COMMON = """local function now_ms() local t=redis.call('TIME') return tonumber(t[1])*1000+math.floor(tonumber(t[2])/1000) end
local function fields(key) local r=redis.call('HGETALL',key) local d={} for i=1,#r,2 do d[r[i]]=r[i+1] end return d end
local function lease(job,effect,owner,fence,until_ms,stage) return {'lease',job,effect,owner,tostring(fence),tostring(until_ms),stage} end
local function record(job,effect,d,status,stage) return {'record',job,effect,status,stage,d['owner'] or '',tostring(d['fence'] or '0'),d['lease_until_ms'] or '',d['result'] or ''} end
"""

# ``begin_job_effect`` atomically creates or claims one ``{job_id, effect}`` stage. A live lease
# held by another owner is reported as ``busy`` instead of being stolen; an expired lease is
# resumed with a strictly higher fence, except after ``external_started`` where the outcome of the
# external call is unknown and the stage becomes terminal ``uncertain`` instead of being replayed.
EFFECT_BEGIN_SCRIPT = _EFFECT_COMMON + """local key=KEYS[1]
local job=ARGV[1] local effect=ARGV[2] local owner=ARGV[3]
local lease_ms=tonumber(ARGV[4]) local ttl=tonumber(ARGV[5])
local now=now_ms()
local r=redis.call('HGETALL',key)
if #r==0 then
  local until_ms=now+lease_ms
  redis.call('HSET',key,'job_id',job,'effect',effect,'owner',owner,'fence','1','stage','claimed','status','running','lease_until_ms',tostring(until_ms))
  if ttl>0 then redis.call('EXPIRE',key,ttl) end
  return lease(job,effect,owner,1,until_ms,'claimed')
end
local d=fields(key)
local status=d['status'] or ''
if status=='done' or status=='uncertain' then return record(job,effect,d,status,d['stage'] or status) end
local held=tonumber(d['lease_until_ms'] or '0') or 0
if held>now then
  if d['owner']==owner then return lease(job,effect,owner,d['fence'],held,d['stage']) end
  return record(job,effect,d,'busy',d['stage'] or 'claimed')
end
if d['stage']=='external_started' then
  redis.call('HSET',key,'status','uncertain','stage','uncertain','lease_until_ms','')
  if ttl>0 then redis.call('EXPIRE',key,ttl) end
  return record(job,effect,d,'uncertain','uncertain')
end
local fence=tonumber(d['fence'] or '0')+1
local until_ms=now+lease_ms
redis.call('HSET',key,'owner',owner,'fence',tostring(fence),'stage','claimed','status','running','lease_until_ms',tostring(until_ms))
redis.call('HDEL',key,'result')
if ttl>0 then redis.call('EXPIRE',key,ttl) end
return lease(job,effect,owner,fence,until_ms,'claimed')"""

EFFECT_RENEW_SCRIPT = _EFFECT_COMMON + """local key=KEYS[1]
local job=ARGV[1] local effect=ARGV[2] local owner=ARGV[3] local fence=ARGV[4]
local lease_ms=tonumber(ARGV[5])
local now=now_ms()
local r=redis.call('HGETALL',key)
if #r==0 then return {'__missing__'} end
local d=fields(key)
if d['owner']~=owner or tostring(d['fence'] or '')~=fence or (d['status'] or '')~='running' then return {'__stale__'} end
local until_ms=now+lease_ms
redis.call('HSET',key,'lease_until_ms',tostring(until_ms))
return lease(job,effect,owner,fence,until_ms,d['stage'])"""

EFFECT_EXTERNAL_SCRIPT = _EFFECT_COMMON + """local key=KEYS[1]
local job=ARGV[1] local effect=ARGV[2] local owner=ARGV[3] local fence=ARGV[4] local ttl=tonumber(ARGV[5])
local r=redis.call('HGETALL',key)
if #r==0 then return {'__missing__'} end
local d=fields(key)
if d['owner']~=owner or tostring(d['fence'] or '')~=fence then return {'__stale__'} end
local status=d['status'] or ''
if status=='done' or status=='uncertain' then return record(job,effect,d,status,d['stage'] or status) end
if status~='running' then return {'__stale__'} end
if d['stage']~='external_started' then redis.call('HSET',key,'stage','external_started') end
if ttl>0 then redis.call('EXPIRE',key,ttl) end
return lease(job,effect,owner,fence,tonumber(d['lease_until_ms'] or '0') or 0,'external_started')"""

EFFECT_COMPLETE_SCRIPT = _EFFECT_COMMON + """local key=KEYS[1]
local job=ARGV[1] local effect=ARGV[2] local owner=ARGV[3] local fence=ARGV[4]
local result=ARGV[5] local ttl=tonumber(ARGV[6])
local r=redis.call('HGETALL',key)
if #r==0 then return {'__missing__'} end
local d=fields(key)
if d['owner']~=owner or tostring(d['fence'] or '')~=fence or (d['status'] or '')~='running' then return {'__stale__'} end
redis.call('HSET',key,'status','done','stage','completed','result',result,'lease_until_ms','')
if ttl>0 then redis.call('EXPIRE',key,ttl) end
d['result']=result d['lease_until_ms']=''
return record(job,effect,d,'done','completed')"""

EFFECT_UNCERTAIN_SCRIPT = _EFFECT_COMMON + """local key=KEYS[1]
local job=ARGV[1] local effect=ARGV[2] local owner=ARGV[3] local fence=ARGV[4]
local result=ARGV[5] local ttl=tonumber(ARGV[6])
local r=redis.call('HGETALL',key)
if #r==0 then return {'__missing__'} end
local d=fields(key)
if d['owner']~=owner or tostring(d['fence'] or '')~=fence or (d['status'] or '')~='running' then return {'__stale__'} end
redis.call('HSET',key,'status','uncertain','stage','uncertain','result',result,'lease_until_ms','')
if ttl>0 then redis.call('EXPIRE',key,ttl) end
d['result']=result d['lease_until_ms']=''
return record(job,effect,d,'uncertain','uncertain')"""

ARTIFACT_SLOT_LIMIT = 1000
ARTIFACT_CLAIM_ATTEMPTS = 8
ARTIFACT_TERMINAL_STATES = frozenset({"published", "uncertain"})
ARTIFACT_PUBLISH_CLAIM_STATES = frozenset({"stream_complete"})
ARTIFACT_PUBLISHED_STATES = frozenset({"publishing", "physically_published"})
ARTIFACT_STREAM_COMPLETE_STATES = frozenset({"prepared", "external_started"})


def _relative_posix(value: Any, what: str = "invalid artifact path") -> PurePosixPath:
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value:
        raise ValueError(what)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(what)
    if ":" in path.parts[0]:
        raise ValueError(what)
    return path


def _normalized_extension(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 12 or "\\" in value or "/" in value:
        raise ValueError("invalid artifact extension")
    extension = value if value.startswith(".") else "." + value
    if not extension[1:].isalnum():
        raise ValueError("invalid artifact extension")
    return extension.lower()


def artifact_slot_target(base: PurePosixPath, extension: str, slot: int) -> PurePosixPath:
    """Return the deterministic target path for one allocation slot."""
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 1:
        raise ValueError("invalid artifact slot")
    name = base.name
    stem = name[: -len(extension)] if name.lower().endswith(extension.lower()) else name
    return base.parent / f"{stem}{'' if slot == 1 else f' ({slot})'}{extension}"


def artifact_target_path(media_root: str | Path, base: PurePosixPath, extension: str, slot: int) -> Path:
    root = Path(media_root)
    return root.joinpath(*artifact_slot_target(base, extension, slot).parts)


def artifact_first_free_slot(media_root: str | Path, base: PurePosixPath, extension: str, ledger_slots: set[int], *, limit: int = ARTIFACT_SLOT_LIMIT) -> int:
    """Return the lowest slot that neither the on-disk layout nor the ledger claims."""
    for slot in range(1, limit + 1):
        if slot in ledger_slots:
            continue
        target = artifact_target_path(media_root, base, extension, slot)
        if target.exists() or target.is_symlink():
            continue
        return slot
    raise ValueError("artifact slots exhausted")


def artifact_takeover_action(record: ArtifactRecord, *, new_owner: str, new_fence: int, now_ms: int) -> ArtifactTakeoverAction:
    """Decide how a higher-fence owner may resume the exact persisted artifact."""
    if isinstance(new_fence, bool) or not isinstance(new_fence, int) or new_fence <= record.fence:
        raise ValueError("stale artifact fence")
    # Terminal states leave only idempotent verification work, so a live lease no longer blocks
    # a takeover; a live lease only protects records that still owe an external call.
    if record.state == "published":
        return "replay"
    if record.state == "uncertain":
        return "uncertain"
    if record.lease_until_ms is not None and record.lease_until_ms > now_ms and record.owner != new_owner:
        return "owner_conflict"
    if record.state == "stream_complete":
        return "verify_publish"
    if record.state in {"publishing", "physically_published"}:
        return "verify_complete"
    if record.state == "external_started":
        return "uncertain"
    return "resume"


def _normalized_hash(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, (list, tuple)):
        if len(raw) == 1:
            return {}
        if len(raw) % 2:
            raise StateUnavailable()
        items = zip(raw[0::2], raw[1::2])
    else:
        raise StateUnavailable()
    values: dict[str, str] = {}
    for key, value in items:
        values[_string(key)] = _string(value)
    return values


def artifact_record_from_hash(values: dict[str, str]) -> ArtifactRecord | None:
    if not values or not values.get("job_id"):
        return None

    def optional_int(name: str) -> int | None:
        value = values.get(name)
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise StateUnavailable() from exc

    return ArtifactRecord(
        job_id=values["job_id"],
        candidate_id=values.get("candidate_id", ""),
        temporary_relative_path=values.get("temporary_relative_path", ""),
        target_relative_path=values.get("target_relative_path", ""),
        allocation_slot=optional_int("allocation_slot") or 1,
        extension=values.get("extension", ""),
        media_type=values.get("media_type", ""),
        declared_size=optional_int("declared_size"),
        size_bytes=optional_int("size_bytes"),
        sha256=values.get("sha256") or None,
        owner=values.get("owner") or None,
        fence=optional_int("fence") or 0,
        lease_until_ms=optional_int("lease_until_ms"),
        state=values.get("state") or "prepared",
    )


def _string(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _effect_int(value: str, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise StateUnavailable() from exc


def _decoded_effect_result(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise StateUnavailable() from exc
    if not isinstance(payload, dict):
        raise StateUnavailable()
    return payload


def job_effect_from_hash(values: dict[str, str]) -> JobEffect | None:
    if not values or not values.get("job_id"):
        return None
    return JobEffect(
        job_id=values["job_id"],
        effect=values.get("effect", ""),
        status=values.get("status", ""),
        stage=values.get("stage", ""),
        owner=values.get("owner") or None,
        fence=_effect_int(values.get("fence"), 0) or 0,
        lease_until_ms=_effect_int(values.get("lease_until_ms")),
        result=_decoded_effect_result(values.get("result")),
    )


def _effect_response(raw: Any) -> EffectLease | JobEffect | None:
    """Decode one effect-script reply, mapping stale owners and fences to `EffectConflict`."""
    if not isinstance(raw, (list, tuple)) or not raw:
        raise StateUnavailable()
    items = [_string(value) for value in raw]
    tag = items[0]
    if tag == "__missing__":
        return None
    if tag == "__stale__":
        raise EffectConflict()
    if tag == "lease" and len(items) == 7:
        _, job_id, effect, owner, fence, until_ms, stage = items
        return EffectLease(job_id=job_id, effect=effect, owner=owner,
                           fence=_effect_int(fence, 0) or 0,
                           lease_until_ms=_effect_int(until_ms, 0) or 0, stage=stage)
    if tag == "record" and len(items) == 9:
        _, job_id, effect, status, stage, owner, fence, until_ms, result = items
        if status not in {"busy", "done", "uncertain"}:
            raise StateUnavailable()
        return JobEffect(job_id=job_id, effect=effect, status=status, stage=stage,
                         owner=owner or None, fence=_effect_int(fence, 0) or 0,
                         lease_until_ms=_effect_int(until_ms), result=_decoded_effect_result(result))
    raise StateUnavailable()


def _require_effect_lease(response: EffectLease | JobEffect | None) -> EffectLease:
    if isinstance(response, EffectLease):
        return response
    raise EffectConflict()


def _require_job_effect(response: EffectLease | JobEffect | None) -> JobEffect:
    if isinstance(response, JobEffect):
        return response
    raise EffectConflict()


def _encode_effect_result(result: Any) -> str:
    """Validate and canonicalize a redacted effect result before it is stored."""
    if not isinstance(result, dict) or len(result) > EFFECT_RESULT_MAX_FIELDS:
        raise ValueError("invalid effect result")
    redacted: dict[str, Any] = {}
    for key, value in result.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 64:
            raise ValueError("invalid effect result")
        if value is None or isinstance(value, bool) or isinstance(value, int):
            redacted[key] = value
        elif isinstance(value, float):
            if value != value or value in {float("inf"), float("-inf")}:
                raise ValueError("invalid effect result")
            redacted[key] = value
        elif isinstance(value, str) and len(value) <= EFFECT_RESULT_MAX_STRING:
            redacted[key] = value
        else:
            raise ValueError("invalid effect result")
    encoded = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > EFFECT_RESULT_MAX_BYTES:
        raise ValueError("invalid effect result")
    return encoded


def _validate_effect(effect: Any) -> str:
    if not isinstance(effect, str) or effect not in EFFECT_NAMES:
        raise ValueError("invalid job effect")
    return effect


def _validate_effect_owner(owner: Any) -> None:
    if not isinstance(owner, str) or not owner or len(owner) > 256:
        raise ValueError("invalid effect owner")


def _validate_effect_fence(fence: Any) -> None:
    if isinstance(fence, bool) or not isinstance(fence, int) or fence < 1:
        raise ValueError("invalid effect fence")


def _validate_effect_lease_ms(lease_ms: Any) -> None:
    if isinstance(lease_ms, bool) or not isinstance(lease_ms, int) or lease_ms < 1:
        raise ValueError("invalid effect lease")


def _validate_effect_code(code: Any) -> None:
    if not isinstance(code, str) or not 1 <= len(code) <= 64 or not code.replace("_", "").isalnum():
        raise ValueError("invalid effect code")


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

    # --- artifact ledger -------------------------------------------------

    def artifact_key(self, job_id: str) -> str:
        return f"{self.namespace}:artifact:{job_id}"

    def artifact_slot_key(self, base_hash: str) -> str:
        return f"{self.namespace}:artifact-slots:{base_hash}"

    async def _hash(self, key: str) -> dict[str, str]:
        try:
            raw = await self.client.hgetall(key)
        except Exception as exc:
            raise StateUnavailable() from exc
        return _normalized_hash(raw)

    async def get_artifact(self, job_id: str) -> ArtifactRecord | None:
        _validate_job_id(job_id)
        return artifact_record_from_hash(await self._hash(self.artifact_key(job_id)))

    async def prepare_artifact(
        self,
        job_id: str,
        candidate: Candidate,
        *,
        media_root: str | Path,
        base_relative_path: str,
        extension: str,
        media_type: str,
        declared_size: int | None,
        owner: str,
        fence: int,
        ttl: int,
    ) -> ArtifactRecord:
        _validate_job_id(job_id)
        if not isinstance(candidate, Candidate):
            raise ValueError("invalid artifact candidate")
        _validate_owner_fence(owner, fence)
        _validate_ttl(ttl)
        if isinstance(declared_size, bool) or not (declared_size is None or (isinstance(declared_size, int) and declared_size > 0)):
            raise ValueError("invalid artifact size")
        if not isinstance(media_type, str) or not 1 <= len(media_type) <= 128:
            raise ValueError("invalid artifact media type")
        base = _relative_posix(base_relative_path)
        extension = _normalized_extension(extension)
        existing = await self.get_artifact(job_id)
        if existing is not None:
            return existing
        base_hash = hashlib.sha256(base.as_posix().encode()).hexdigest()
        slots_key = self.artifact_slot_key(base_hash)
        staging = f".musicdl-staging/{hashlib.sha256(job_id.encode()).hexdigest()[:40]}.{fence}.part"
        for _ in range(ARTIFACT_CLAIM_ATTEMPTS):
            ledger_slots = set()
            for key in await self._hash(slots_key):
                try:
                    ledger_slots.add(int(key))
                except (TypeError, ValueError):
                    continue
            slot = artifact_first_free_slot(media_root, base, extension, ledger_slots)
            target = artifact_slot_target(base, extension, slot).as_posix()
            result = await self._eval(
                CLAIM_ARTIFACT_SCRIPT, [slots_key, self.artifact_key(job_id)],
                str(slot), job_id, candidate.item_id, base_hash, target, staging, extension, media_type,
                "" if declared_size is None else str(declared_size), owner, str(fence), str(ttl))
            claimed = artifact_record_from_hash(_normalized_hash(result))
            if claimed is not None:
                return claimed
        raise StateUnavailable()

    async def _artifact_transition(
        self,
        job_id: str,
        *,
        allowed: frozenset[str] | set[str],
        owner: str = "",
        fence: int | None = None,
        state: str = "",
        ttl: int = 0,
        size_bytes: int | None = None,
        sha256: str | None = None,
        extension: str | None = None,
        media_type: str | None = None,
        declared_size: int | None = None,
        new_owner: str = "",
        new_fence: int | None = None,
        lease_until_ms: int | None = None,
    ) -> ArtifactRecord:
        _validate_job_id(job_id)
        _validate_ttl(ttl)
        result = await self._eval(
            ARTIFACT_TRANSITION_SCRIPT,
            [self.artifact_key(job_id)],
            "" if owner is None else str(owner),
            "" if fence is None else str(fence),
            ",".join(sorted(allowed)),
            state,
            str(ttl),
            "" if size_bytes is None else str(size_bytes),
            sha256 or "",
            extension or "",
            media_type or "",
            "" if declared_size is None else str(declared_size),
            "" if new_owner is None else str(new_owner),
            "" if new_fence is None else str(new_fence),
            "" if lease_until_ms is None else str(lease_until_ms),
        )
        record = artifact_record_from_hash(_normalized_hash(result))
        if record is None:
            raise ArtifactConflict()
        return record

    async def record_stream_complete(
        self,
        job_id: str,
        *,
        owner: str,
        fence: int,
        size_bytes: int,
        sha256: str,
        extension: str,
        media_type: str,
        declared_size: int | None,
        ttl: int,
    ) -> ArtifactRecord:
        _validate_owner_fence(owner, fence)
        return await self._artifact_transition(
            job_id, allowed=ARTIFACT_STREAM_COMPLETE_STATES, owner=owner, fence=fence,
            state="stream_complete", ttl=ttl, size_bytes=size_bytes, sha256=sha256,
            extension=extension, media_type=media_type, declared_size=declared_size)

    async def claim_artifact_publish(self, job_id: str, *, owner: str, fence: int, ttl: int) -> ArtifactRecord:
        _validate_owner_fence(owner, fence)
        return await self._artifact_transition(
            job_id, allowed=ARTIFACT_PUBLISH_CLAIM_STATES, owner=owner, fence=fence, state="publishing", ttl=ttl)

    async def mark_artifact_published(self, job_id: str, *, owner: str, fence: int, ttl: int) -> ArtifactRecord:
        _validate_owner_fence(owner, fence)
        return await self._artifact_transition(
            job_id, allowed=ARTIFACT_PUBLISHED_STATES, owner=owner, fence=fence, state="published", ttl=ttl)

    async def takeover_artifact(
        self,
        job_id: str,
        *,
        new_owner: str,
        new_fence: int,
        now_ms: int,
        lease_ms: int,
    ) -> ArtifactTakeoverAction:
        _validate_job_id(job_id)
        _validate_owner_fence(new_owner, new_fence)
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise ValueError("invalid artifact clock")
        if isinstance(lease_ms, bool) or not isinstance(lease_ms, int) or lease_ms < 0:
            raise ValueError("invalid artifact lease")
        record = await self.get_artifact(job_id)
        if record is None:
            return "resume"
        action = artifact_takeover_action(record, new_owner=new_owner, new_fence=new_fence, now_ms=now_ms)
        if action in {"owner_conflict", "replay"}:
            return action
        await self._artifact_transition(
            job_id, allowed={record.state}, fence=record.fence,
            state="uncertain" if action == "uncertain" else record.state,
            new_owner=new_owner, new_fence=new_fence,
            lease_until_ms=None if action == "uncertain" else now_ms + lease_ms)
        return action

    # --- fenced job-effect state machine ---------------------------------

    def effect_key(self, job_id: str, effect: str) -> str:
        return f"{self.namespace}:effect:{job_id}:{effect}"

    async def get_job_effect(self, job_id: str, effect: str) -> JobEffect | None:
        _validate_job_id(job_id)
        _validate_effect(effect)
        return job_effect_from_hash(await self._hash(self.effect_key(job_id, effect)))

    async def begin_job_effect(
        self,
        job_id: str,
        effect: str,
        owner: str,
        *,
        lease_ms: int,
        ttl: int,
    ) -> EffectLease | JobEffect:
        _validate_job_id(job_id)
        _validate_effect(effect)
        _validate_effect_owner(owner)
        _validate_effect_lease_ms(lease_ms)
        _validate_ttl(ttl)
        result = await self._eval(
            EFFECT_BEGIN_SCRIPT, [self.effect_key(job_id, effect)],
            job_id, effect, owner, str(lease_ms), str(ttl))
        response = _effect_response(result)
        if response is None:
            raise StateUnavailable()
        return response

    async def renew_job_effect(
        self,
        job_id: str,
        effect: str,
        owner: str,
        fence: int,
        *,
        lease_ms: int,
    ) -> EffectLease:
        _validate_job_id(job_id)
        _validate_effect(effect)
        _validate_effect_owner(owner)
        _validate_effect_fence(fence)
        _validate_effect_lease_ms(lease_ms)
        result = await self._eval(
            EFFECT_RENEW_SCRIPT, [self.effect_key(job_id, effect)],
            job_id, effect, owner, str(fence), str(lease_ms))
        return _require_effect_lease(_effect_response(result))

    async def begin_external_effect(
        self,
        job_id: str,
        effect: str,
        owner: str,
        fence: int,
        *,
        ttl: int,
    ) -> EffectLease:
        _validate_job_id(job_id)
        _validate_effect(effect)
        _validate_effect_owner(owner)
        _validate_effect_fence(fence)
        _validate_ttl(ttl)
        result = await self._eval(
            EFFECT_EXTERNAL_SCRIPT, [self.effect_key(job_id, effect)],
            job_id, effect, owner, str(fence), str(ttl))
        return _require_effect_lease(_effect_response(result))

    async def complete_job_effect(
        self,
        job_id: str,
        effect: str,
        owner: str,
        fence: int,
        result: dict[str, Any],
        *,
        ttl: int,
    ) -> JobEffect:
        _validate_job_id(job_id)
        _validate_effect(effect)
        _validate_effect_owner(owner)
        _validate_effect_fence(fence)
        _validate_ttl(ttl)
        encoded = _encode_effect_result(result)
        reply = await self._eval(
            EFFECT_COMPLETE_SCRIPT, [self.effect_key(job_id, effect)],
            job_id, effect, owner, str(fence), encoded, str(ttl))
        return _require_job_effect(_effect_response(reply))

    async def mark_job_effect_uncertain(
        self,
        job_id: str,
        effect: str,
        owner: str,
        fence: int,
        code: str,
        *,
        ttl: int,
    ) -> JobEffect:
        _validate_job_id(job_id)
        _validate_effect(effect)
        _validate_effect_owner(owner)
        _validate_effect_fence(fence)
        _validate_effect_code(code)
        _validate_ttl(ttl)
        reply = await self._eval(
            EFFECT_UNCERTAIN_SCRIPT, [self.effect_key(job_id, effect)],
            job_id, effect, owner, str(fence), _encode_effect_result({"code": code}), str(ttl))
        return _require_job_effect(_effect_response(reply))

    async def redis_now_ms(self) -> int:
        """Read the state store clock so every lease decision uses Redis time."""
        value = await self._eval("return redis.call('TIME')", [])
        if isinstance(value, (list, tuple)) and value:
            return int(_string(value[0])) * 1000 + int(_string(value[1])) // 1000
        raise StateUnavailable()


def _validate_job_id(job_id: Any) -> str:
    if not isinstance(job_id, str) or not job_id or len(job_id) > 256:
        raise ValueError("invalid job id")
    return job_id


def _validate_owner_fence(owner: Any, fence: Any) -> None:
    if not isinstance(owner, str) or not owner or len(owner) > 256:
        raise ValueError("invalid artifact owner")
    if isinstance(fence, bool) or not isinstance(fence, int) or fence < 1:
        raise ValueError("invalid artifact fence")


def _validate_ttl(ttl: Any) -> None:
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0:
        raise ValueError("invalid artifact ttl")
