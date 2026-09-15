from __future__ import annotations
import hashlib, json
from typing import Any
from musicdl.wecom.state import SelectionContext, freeze_candidates, selection_payload

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

async def get_selection_for_request(redis: Any, request_id: str, *, namespace: str = "{musicdl}") -> dict[str, Any] | None:
    raw = await redis.get(_request_key(namespace, request_id))
    if isinstance(raw, bytes): raw = raw.decode()
    return json.loads(raw) if raw else None
