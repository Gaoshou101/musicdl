from __future__ import annotations

import asyncio
from typing import Any
import httpx


class WeComError(RuntimeError):
    """A rejected or malformed WeCom API response."""


class WeComClient:
    def __init__(self, corp_id: str, secret: str, agent_id: int, redis: Any, *,
                 base_url: str = "https://qyapi.weixin.qq.com", namespace: str = "{musicdl}",
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0):
        self.corp_id, self.secret, self.agent_id, self.redis = corp_id, secret, agent_id, redis
        self.base_url = base_url.rstrip("/")
        self.cache_key = f"{namespace}:wecom:access_token"
        self.transport, self.timeout = transport, timeout
        self._token_lock = asyncio.Lock()

    async def _cached_token(self) -> str | None:
        try:
            cached = await self.redis.get(self.cache_key)
            if isinstance(cached, bytes):
                cached = cached.decode("utf-8")
            if cached is None:
                return None
            if not isinstance(cached, str) or not cached:
                raise ValueError("invalid cached token")
            return cached
        except Exception:
            raise WeComError("token request failed") from None

    async def _request_data(self, method: str, url: str, error: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                response = await client.request(method, url, **kwargs)
                response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("response must be an object")
            return data
        except Exception:
            raise WeComError(error) from None

    async def access_token(self, *, force_refresh: bool = False, stale_token: str | None = None) -> str:
        if not force_refresh:
            cached = await self._cached_token()
            if cached:
                return cached
        async with self._token_lock:
            cached = await self._cached_token()
            if cached and (not force_refresh or (stale_token is not None and cached != stale_token)):
                return cached
            data = await self._request_data(
                "GET", self.base_url + "/cgi-bin/gettoken", "token request failed",
                params={"corpid": self.corp_id, "corpsecret": self.secret},
            )
            if data.get("errcode", 0) != 0 or not isinstance(data.get("access_token"), str) or not data["access_token"]:
                raise WeComError("token request failed")
            token = data["access_token"]
            try:
                ttl = max(1, int(data.get("expires_in", 7200)) - 60)
                await self.redis.set(self.cache_key, token, ex=ttl)
            except Exception:
                raise WeComError("token request failed") from None
            return token

    async def send_text(self, user: str, content: str) -> dict[str, Any]:
        try:
            invalid_content = not isinstance(content, str) or not content or len(content.encode("utf-8")) > 2048
        except UnicodeEncodeError:
            invalid_content = True
        if not isinstance(user, str) or not user or len(user) > 128 or invalid_content:
            raise ValueError("invalid message")
        token = await self.access_token()
        return await self._send_with_token(user, content, token, retry=True)

    async def _send_with_token(self, user: str, content: str, token: str, *, retry: bool) -> dict[str, Any]:
        payload = {"touser": user, "msgtype": "text", "agentid": self.agent_id, "text": {"content": content}}
        data = await self._request_data(
            "POST", self.base_url + "/cgi-bin/message/send", "message send failed",
            params={"access_token": token}, json=payload,
        )
        if data.get("errcode", 0) in {40014, 42001} and retry:
            refreshed = await self.access_token(force_refresh=True, stale_token=token)
            return await self._send_with_token(user, content, refreshed, retry=False)
        if data.get("errcode", 0) != 0:
            raise WeComError("message send failed")
        return data

    send_message = send_text
