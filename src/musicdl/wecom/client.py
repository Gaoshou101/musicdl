from __future__ import annotations

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

    async def access_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = await self.redis.get(self.cache_key)
            if cached:
                return cached.decode() if isinstance(cached, bytes) else str(cached)
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout, follow_redirects=False) as client:
            response = await client.get(self.base_url + "/cgi-bin/gettoken", params={"corpid": self.corp_id, "corpsecret": self.secret})
            try: response.raise_for_status()
            except httpx.HTTPError: raise WeComError("token request failed") from None
        data = response.json()
        if data.get("errcode", 0) != 0 or not isinstance(data.get("access_token"), str):
            raise WeComError("token request failed")
        token = data["access_token"]
        ttl = max(1, int(data.get("expires_in", 7200)) - 60)
        await self.redis.set(self.cache_key, token, ex=ttl)
        return token

    async def send_text(self, user: str, content: str) -> dict[str, Any]:
        if not isinstance(user, str) or not user or len(user) > 128 or not isinstance(content, str) or not content or len(content) > 2048:
            raise ValueError("invalid message")
        token = await self.access_token()
        return await self._send_with_token(user, content, token, retry=True)

    async def _send_with_token(self, user: str, content: str, token: str, *, retry: bool) -> dict[str, Any]:
        payload = {"touser": user, "msgtype": "text", "agentid": self.agent_id, "text": {"content": content}}
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout, follow_redirects=False) as client:
            response = await client.post(self.base_url + "/cgi-bin/message/send", params={"access_token": token}, json=payload)
            try: response.raise_for_status()
            except httpx.HTTPError: raise WeComError("message send failed") from None
        data = response.json()
        if data.get("errcode", 0) == 40014 and retry:
            return await self._send_with_token(user, content, await self.access_token(force_refresh=True), retry=False)
        if data.get("errcode", 0) != 0:
            raise WeComError("message send failed")
        return data

    send_message = send_text
