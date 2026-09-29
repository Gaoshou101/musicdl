from __future__ import annotations
import hashlib, json
from typing import Any
from musicdl.wecom.state import SelectionContext, SelectionRejected, freeze_candidates, selection_payload


# Navigation updates the display cursor, never the frozen download snapshot.
# Replaying one stream delivery must return the same page rather than advance it.
_PAGE_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return -1 end
local ttl = redis.call('TTL', KEYS[2])
if ttl <= 0 then return -1 end
local prior = redis.call('GET', KEYS[4])
if prior then return math.max(0, math.min(tonumber(ARGV[3]) - 1, tonumber(prior))) end
local page = tonumber(redis.call('GET', KEYS[3]) or '0')
page = math.max(0, math.min(tonumber(ARGV[3]) - 1, page + tonumber(ARGV[2])))
redis.call('SET', KEYS[3], page, 'EX', ttl)
redis.call('SET', KEYS[4], page, 'EX', ttl)
return page
"""

_CANCEL_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1], KEYS[2], KEYS[3])
return 1
"""

_INTERACTION_SCRIPT = """
local prior = redis.call('GET', KEYS[2])
if prior then return prior end
local token = redis.call('GET', KEYS[1]) or ''
redis.call('SET', KEYS[2], token, 'EX', ARGV[1])
return token
"""

def _hash(value: str) -> str: return hashlib.sha256(value.encode()).hexdigest()
def _key(namespace: str, token: str) -> str: return f"{namespace}:worker-selection:{_hash(token)}"
def _user_key(namespace: str, corp_id: str, user: str) -> str: return f"{namespace}:worker-user:{_hash(corp_id + ':' + user)}"
def _request_key(namespace: str, request_id: str) -> str: return f"{namespace}:worker-request:{_hash(request_id)}"

async def bind_user_selection(redis: Any, token: str, context: SelectionContext, *, query: str | None = None, candidates: dict | None = None, ttl: int = 900, namespace: str = "{musicdl}") -> None:
    """Bind one token to the durable user/request routes and the frozen candidate snapshot."""
    data = selection_payload(context)
    if query is not None:
        if not isinstance(query, str) or len(query) > 512:
            raise ValueError("invalid selection context")
        data["query"] = query
    if candidates is not None:
        data["candidates"] = freeze_candidates(candidates)
    encoded = json.dumps({**data, "token": token}, ensure_ascii=False)
    expiry = min(max(ttl, 60), 86400)
    keys = [_key(namespace, token), _user_key(namespace, context.corp_id, context.from_user), _request_key(namespace, context.request_id)]
    if hasattr(redis, "eval"):
        script = "local value = ARGV[1]; local token = ARGV[2]; local ttl = tonumber(ARGV[3]); redis.call('SET', KEYS[1], value, 'EX', ttl); redis.call('SET', KEYS[2], token, 'EX', ttl); redis.call('SET', KEYS[3], value, 'EX', ttl); return 1"
        await redis.eval(script, len(keys), *keys, encoded, token, expiry)
        return
    # Minimal fakes used by unit tests do not implement EVAL; real Redis always does.
    await redis.set(keys[0], encoded, ex=expiry)
    await redis.set(keys[1], token, ex=expiry)
    await redis.set(keys[2], encoded, ex=expiry)

async def _get_by_token(redis: Any, token: str, *, namespace: str = "{musicdl}") -> dict[str, Any] | None:
    raw = await redis.get(_key(namespace, token))
    if not raw: return None
    if isinstance(raw, bytes): raw = raw.decode()
    return json.loads(raw)

async def get_selection_for_user(redis: Any, corp_id: str, user: str, *, namespace: str = "{musicdl}") -> dict[str, Any] | None:
    token = await redis.get(_user_key(namespace, corp_id, user))
    if isinstance(token, bytes): token = token.decode()
    return await _get_by_token(redis, token, namespace=namespace) if token else None

async def get_user_selection(redis: Any, corp_id: str, from_user: str, *, namespace: str = "{musicdl}") -> dict[str, Any] | None:
    return await get_selection_for_user(redis, corp_id, from_user, namespace=namespace)


async def get_interaction_selection(redis: Any, corp_id: str, user: str, request_id: str, *,
                                    ttl: int, namespace: str = "{musicdl}") -> dict[str, Any] | None:
    """A retried numeric reply or cancellation must not target a newer search."""
    identity = json.dumps([corp_id, user, request_id], separators=(",", ":"))
    keys = [_user_key(namespace, corp_id, user), f"{namespace}:worker-interaction:{_hash(identity)}"]
    token = await redis.eval(_INTERACTION_SCRIPT, len(keys), *keys, ttl)
    if isinstance(token, bytes):
        token = token.decode()
    return await _get_by_token(redis, token, namespace=namespace) if token else None

async def get_selection_for_request(redis: Any, request_id: str, *, namespace: str = "{musicdl}") -> dict[str, Any] | None:
    raw = await redis.get(_request_key(namespace, request_id))
    if isinstance(raw, bytes): raw = raw.decode()
    return json.loads(raw) if raw else None


async def move_selection_page(redis: Any, token: str, context: SelectionContext, *,
                              request_id: str, direction: int, page_count: int,
                              namespace: str = "{musicdl}") -> int:
    """Move a still-active selection once per callback, within its original TTL."""
    if (direction not in (-1, 1) or isinstance(direction, bool)
            or isinstance(page_count, bool) or not isinstance(page_count, int)
            or not 1 <= page_count <= 100):
        raise ValueError("invalid selection page")
    digest = _hash(token)
    keys = [_user_key(namespace, context.corp_id, context.from_user),
            f"{namespace}:selection:{digest}", f"{namespace}:selection-page:{digest}",
            f"{namespace}:selection-navigation:{_hash(token + ':' + request_id)}"]
    page = int(await redis.eval(_PAGE_SCRIPT, len(keys), *keys, token, direction, page_count))
    if page < 0:
        raise SelectionRejected()
    return page


async def cancel_user_selection(redis: Any, token: str, context: SelectionContext, *,
                                namespace: str = "{musicdl}") -> bool:
    """Cancel only this token; retain request snapshots used by queued jobs."""
    digest = _hash(token)
    keys = [_user_key(namespace, context.corp_id, context.from_user),
            f"{namespace}:selection:{digest}", f"{namespace}:selection-page:{digest}"]
    return bool(await redis.eval(_CANCEL_SCRIPT, len(keys), *keys, token))
