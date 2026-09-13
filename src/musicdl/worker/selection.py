from __future__ import annotations
import hashlib, json
from typing import Any
from musicdl.wecom.state import SelectionContext

def _hash(value: str) -> str: return hashlib.sha256(value.encode()).hexdigest()
def _key(namespace: str, token: str) -> str: return f"{namespace}:worker-selection:{_hash(token)}"
def _user_key(namespace: str, corp_id: str, user: str) -> str: return f"{namespace}:worker-user:{_hash(corp_id + ':' + user)}"
def _request_key(namespace: str, request_id: str) -> str: return f"{namespace}:worker-request:{_hash(request_id)}"

async def bind_user_selection(redis: Any, token: str, context: SelectionContext, *, query: str = "", candidates: dict | None = None, ttl: int = 900, namespace: str = "{musicdl}") -> None:
    data = {"corp_id": context.corp_id, "from_user": context.from_user, "request_id": context.request_id, "version": context.candidate_set_version, "candidates": candidates or context.candidates, "query": query}
    encoded = json.dumps({**data, "token": token}, ensure_ascii=False)
    await redis.set(_key(namespace, token), encoded, ex=min(max(ttl, 60), 86400))
    await redis.set(_user_key(namespace, context.corp_id, context.from_user), token, ex=min(max(ttl, 60), 86400))
    await redis.set(_request_key(namespace, context.request_id), encoded, ex=min(max(ttl, 60), 86400))

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
