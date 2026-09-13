import time
import base64
import asyncio
import logging
import sys
import types
from unittest.mock import AsyncMock
import httpx

import pytest
from pydantic import SecretStr

from musicdl.app import create_app
from musicdl.config import AppSettings, WeComSettings
from musicdl.wecom.crypto import WeComCrypto, compute_signature
from musicdl.wecom.state import StateUnavailable
from musicdl.wecom.service import WeComService, _request_id
from musicdl.wecom.models import WeComMessage
from musicdl.wecom.state import EnqueueResult


KEY = base64.b64encode(b"k" * 32).decode().rstrip("=")


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def settings(**kw):
    w = dict(enabled=True, corp_id="corp", agent_id=7, token=SecretStr("tok"), encoding_aes_key=SecretStr(KEY), allowed_users=["u1"])
    w.update(kw)
    return AppSettings(wecom=WeComSettings(**w))


def envelope(inner, *, ts=None, nonce="n", token="tok"):
    ts = str(int(time.time()) if ts is None else ts)
    crypto = WeComCrypto(KEY, "corp")
    enc = crypto.encrypt(inner, random_prefix=b"r" * 16)
    sig = compute_signature(token, ts, nonce, enc)
    body = f"<xml><ToUserName><![CDATA[corp]]></ToUserName><AgentID>7</AgentID><Encrypt><![CDATA[{enc}]]></Encrypt></xml>".encode()
    return body, {"msg_signature": sig, "timestamp": ts, "nonce": nonce}


@pytest.fixture
def fake_state():
    state = AsyncMock()
    state.ping.return_value = True
    state.lookup_message.return_value = False
    state.enqueue_message.return_value = EnqueueResult("1-0")
    return state


def client(state, **kw):
    app = create_app(settings(**kw), state_factory=lambda _: state, clock=lambda: 1_700_000_000)
    app.state.wecom_state = state
    app.state.wecom_service = WeComService(app.state.wecom_state and settings(**kw).wecom, state, lambda: 1_700_000_000)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_callback_disabled_and_health_independent(fake_state):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(AppSettings(), state_factory=lambda _: fake_state)), base_url="http://test") as c:
            assert (await c.get("/healthz")).status_code == 200
            assert (await c.get("/wecom/callback")).status_code == 503
    asyncio.run(run())


def test_get_validation_returns_exact_plaintext_without_redis(fake_state):
    crypto = WeComCrypto(KEY, "corp")
    ts = "1700000000"
    enc = crypto.encrypt("challenge", random_prefix=b"r" * 16)
    sig = compute_signature("tok", ts, "n", enc)
    async def run():
      async with client(fake_state) as c:
        return await c.get("/wecom/callback", params={"msg_signature": sig, "timestamp": ts, "nonce": "n", "echostr": enc})
    r = asyncio.run(run())
    assert r.status_code == 200 and r.text == "challenge" and r.headers["content-type"].startswith("text/plain")
    fake_state.ping.assert_not_awaited()


def test_get_bad_signature_and_expired_are_400(fake_state):
    crypto = WeComCrypto(KEY, "corp")
    enc = crypto.encrypt("challenge", random_prefix=b"r" * 16)
    async def run():
      async with client(fake_state) as c:
        assert (await c.get("/wecom/callback", params={"msg_signature":"0" * 40, "timestamp":"1700000000", "nonce":"n", "echostr":enc})).status_code == 400
        old = "1699000000"
        sig = compute_signature("tok", old, "n", enc)
        assert (await c.get("/wecom/callback", params={"msg_signature":sig, "timestamp":old, "nonce":"n", "echostr":enc})).status_code == 400
    asyncio.run(run())


def test_post_text_enqueues_and_duplicate_acks(fake_state):
    inner = "<xml><FromUserName><![CDATA[u1]]></FromUserName><MsgType>text</MsgType><Content><![CDATA[/cancel]]></Content><CreateTime>1700000000</CreateTime><MsgId>m1</MsgId><AgentID>7</AgentID></xml>"
    from musicdl.wecom.state import EnqueueResult
    fake_state.enqueue_message.side_effect = [EnqueueResult("1-0"), EnqueueResult("1-0", True)]
    body, params = envelope(inner, ts=1700000000)
    async def run():
      async with client(fake_state) as c:
        first = await c.post("/wecom/callback", params=params, content=body)
        second = await c.post("/wecom/callback", params=params, content=body)
        assert first.status_code == second.status_code == 200
        assert first.content == second.content == b""
    asyncio.run(run())
    assert fake_state.enqueue_message.await_count == 2


def test_post_rejects_compression_and_overlimit(fake_state):
    async def run():
      async with client(fake_state) as c:
        assert (await c.post("/wecom/callback", params={"msg_signature":"x","timestamp":"1","nonce":"n"}, headers={"Content-Encoding":"gzip"}, content=b"x")).status_code == 415
        assert (await c.post("/wecom/callback", params={"msg_signature":"0" * 40,"timestamp":"1700000000","nonce":"n"}, content=b"x" * (64 * 1024 + 1))).status_code == 413
    asyncio.run(run())


def test_streaming_body_limit_is_enforced_without_decrypt_or_enqueue(fake_state, monkeypatch):
    called = False
    def decrypt(self, value):
        nonlocal called
        called = True
        return ""
    monkeypatch.setattr(WeComCrypto, "decrypt", decrypt)
    params = {"msg_signature": "0" * 40, "timestamp": "1700000000", "nonce": "n"}
    async def run():
      app = create_app(settings(), state_factory=lambda _: fake_state, clock=lambda: 1_700_000_000)
      app.state.wecom_state = fake_state
      app.state.wecom_service = WeComService(settings().wecom, fake_state, lambda: 1_700_000_000)
      request = httpx.Request("POST", "http://test/wecom/callback", params=params, stream=Chunks([b"a" * 20000, b"b" * 30000, b"c" * 20000]))
      response = await httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test").send(request)
      return response
    assert asyncio.run(run()).status_code == 413
    assert called is False
    fake_state.enqueue_message.assert_not_awaited()


def test_readyz_redis_failure_is_503(fake_state):
    fake_state.ping.side_effect = StateUnavailable()
    async def run():
      async with client(fake_state) as c:
        assert (await c.get("/readyz")).status_code == 503
    asyncio.run(run())


def test_readyz_enabled_pings_once_and_disabled_skips_state(fake_state):
    async def run():
      async with client(fake_state) as c:
        response = await c.get("/readyz")
        assert response.status_code == 200
    asyncio.run(run())
    fake_state.ping.assert_awaited_once()
    disabled = AsyncMock()
    async def disabled_run():
      app = create_app(AppSettings(), state_factory=lambda _: disabled)
      async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/readyz")).status_code == 200
    asyncio.run(disabled_run())
    disabled.ping.assert_not_awaited()


def test_post_rejects_auth_and_unsupported_without_enqueue(fake_state):
    async def run():
      async with client(fake_state) as c:
        for user, agent in (("other", 7), ("u1", 8)):
            inner = f"<xml><FromUserName><![CDATA[{user}]]></FromUserName><MsgType>text</MsgType><Content><![CDATA[/cancel]]></Content><CreateTime>1700000000</CreateTime><MsgId>m{user}</MsgId><AgentID>{agent}</AgentID></xml>"
            body, params = envelope(inner, ts=1700000000)
            assert (await c.post("/wecom/callback", params=params, content=body)).status_code == 403
        inner = "<xml><FromUserName><![CDATA[u1]]></FromUserName><MsgType>image</MsgType><CreateTime>1700000000</CreateTime><MsgId>im1</MsgId><AgentID>7</AgentID></xml>"
        body, params = envelope(inner, ts=1700000000)
        response = await c.post("/wecom/callback", params=params, content=body)
        assert response.status_code == 200 and response.content == b""
    asyncio.run(run())
    assert fake_state.enqueue_message.await_count == 0


def test_post_rejects_wrong_corp_id(fake_state):
    crypto = WeComCrypto(KEY, "corp")
    ts, nonce = "1700000000", "n"
    enc = crypto.encrypt("<xml><FromUserName>u1</FromUserName><MsgType>image</MsgType><CreateTime>1700000000</CreateTime><MsgId>x</MsgId><AgentID>7</AgentID></xml>", random_prefix=b"r" * 16, receive_id="other-corp")
    body = f"<xml><ToUserName>corp</ToUserName><AgentID>7</AgentID><Encrypt><![CDATA[{enc}]]></Encrypt></xml>".encode()
    params = {"msg_signature": compute_signature("tok", ts, nonce, enc), "timestamp": ts, "nonce": nonce}
    async def run():
      async with client(fake_state) as c:
        return await c.post("/wecom/callback", params=params, content=body)
    assert asyncio.run(run()).status_code == 400


def test_post_bad_signature_never_decrypts(fake_state, monkeypatch):
    called = False
    original = WeComCrypto.decrypt
    def decrypt(self, value):
        nonlocal called
        called = True
        return original(self, value)
    monkeypatch.setattr(WeComCrypto, "decrypt", decrypt)
    body, params = envelope("<xml><FromUserName>u1</FromUserName><MsgType>image</MsgType><CreateTime>1700000000</CreateTime><MsgId>x</MsgId><AgentID>7</AgentID></xml>", ts=1700000000)
    params["msg_signature"] = "0" * 40
    async def run():
      async with client(fake_state) as c:
        assert (await c.post("/wecom/callback", params=params, content=body)).status_code == 400
    asyncio.run(run())
    assert called is False


def test_expired_known_unknown_and_lookup_failure(fake_state):
    inner = "<xml><FromUserName><![CDATA[u1]]></FromUserName><MsgType>text</MsgType><Content><![CDATA[/cancel]]></Content><CreateTime>1600000000</CreateTime><MsgId>old</MsgId><AgentID>7</AgentID></xml>"
    body, params = envelope(inner, ts=1600000000)
    async def run():
      async with client(fake_state) as c:
        fake_state.lookup_message.return_value = True
        assert (await c.post("/wecom/callback", params=params, content=body)).status_code == 200
        fake_state.lookup_message.return_value = False
        assert (await c.post("/wecom/callback", params=params, content=body)).status_code == 400
        fake_state.lookup_message.side_effect = StateUnavailable()
        assert (await c.post("/wecom/callback", params=params, content=body)).status_code == 503
    asyncio.run(run())


def test_enqueue_failure_is_503_and_logs_do_not_contain_payload(fake_state, caplog):
    fake_state.enqueue_message.side_effect = StateUnavailable()
    sentinel = "secret-content-sentinel"
    inner = f"<xml><FromUserName><![CDATA[u1]]></FromUserName><MsgType>text</MsgType><Content><![CDATA[{sentinel}]]></Content><CreateTime>1700000000</CreateTime><MsgId>m2</MsgId><AgentID>7</AgentID></xml>"
    body, params = envelope(inner, ts=1700000000)
    async def run():
      async with client(fake_state) as c:
        return await c.post("/wecom/callback", params=params, content=body)
    response = asyncio.run(run())
    assert response.status_code == 503
    assert sentinel not in caplog.text


def test_audit_log_is_non_sensitive(fake_state, caplog):
    caplog.set_level(logging.INFO, logger="musicdl.wecom")
    content = "content-secret"
    inner = f"<xml><FromUserName><![CDATA[u1]]></FromUserName><MsgType>text</MsgType><Content><![CDATA[{content}]]></Content><CreateTime>1700000000</CreateTime><MsgId>safe-id</MsgId><AgentID>7</AgentID></xml>"
    body, params = envelope(inner, ts=1700000000, nonce="nonce-secret")
    async def run():
      async with client(fake_state) as c:
        return await c.post("/wecom/callback", params=params, content=body)
    assert asyncio.run(run()).status_code == 200
    assert "wecom_callback" in caplog.text
    ciphertext = body.decode().split("Encrypt><![CDATA[")[1].split("]]>")[0]
    for value in (content, "u1", "nonce-secret", params["msg_signature"], ciphertext, "tok", KEY):
        assert value not in caplog.text


def test_audit_log_does_not_emit_raw_request_id_or_newline(fake_state, caplog):
    caplog.set_level(logging.INFO, logger="musicdl.wecom")
    request_id = "msg-id\nINJECTED\tCONTROL"
    inner = f"<xml><FromUserName><![CDATA[u1]]></FromUserName><MsgType>text</MsgType><Content><![CDATA[/cancel]]></Content><CreateTime>1700000000</CreateTime><MsgId><![CDATA[{request_id}]]></MsgId><AgentID>7</AgentID></xml>"
    body, params = envelope(inner, ts=1700000000)
    async def run():
      async with client(fake_state) as c:
        return await c.post("/wecom/callback", params=params, content=body)
    assert asyncio.run(run()).status_code == 200
    assert "wecom_callback" in caplog.text
    assert request_id not in caplog.text
    assert "INJECTED" not in caplog.text
    assert "CONTROL" not in caplog.text
    assert "request_fingerprint=" in caplog.text


def test_event_without_msgid_has_stable_request_id(fake_state):
    inner = "<xml><FromUserName><![CDATA[u1]]></FromUserName><MsgType>image</MsgType><CreateTime>1700000000</CreateTime><AgentID>7</AgentID><Event>click</Event></xml>"
    body, params = envelope(inner, ts=1600000000)
    async def run():
      async with client(fake_state) as c:
        fake_state.lookup_message.return_value = True
        first = await c.post("/wecom/callback", params=params, content=body)
        second = await c.post("/wecom/callback", params=params, content=body)
        return first, second
    first, second = asyncio.run(run())
    assert first.content == second.content == b""
    assert fake_state.enqueue_message.await_count == 0
    ids = [call.args[1] for call in fake_state.lookup_message.await_args_list]
    assert ids[0] == ids[1] and ids[0].startswith("event-")
    assert len(ids[0][6:]) == 64 and all(c in "0123456789abcdef" for c in ids[0][6:])
    base = WeComMessage("u1", "", "image", 7, 1600000000, None, "click")
    assert _request_id(base) != _request_id(WeComMessage("u1", "", "image", 8, 1600000000, None, "click"))


def test_lifespan_initializes_injected_state_and_get_works(fake_state):
    app = create_app(settings(), state_factory=lambda _: fake_state, clock=lambda: 1_700_000_000)
    crypto = WeComCrypto(KEY, "corp")
    enc = crypto.encrypt("life", random_prefix=b"r" * 16)
    ts = "1700000000"
    params = {"msg_signature": compute_signature("tok", ts, "n", enc), "timestamp": ts, "nonce": "n", "echostr": enc}
    async def run():
      async with app.router.lifespan_context(app):
        assert hasattr(app.state, "wecom_service")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get("/wecom/callback", params=params)
        return response
    assert asyncio.run(run()).text == "life"
    fake_state.ping.assert_not_awaited()


def test_lifespan_passes_conservative_redis_timeouts(monkeypatch):
    fake_client = AsyncMock()
    fake_client.ping.return_value = True
    redis_cls = types.SimpleNamespace(from_url=lambda *args, **kwargs: fake_client)
    redis_asyncio = types.ModuleType("redis.asyncio")
    redis_asyncio.Redis = redis_cls
    redis_pkg = types.ModuleType("redis")
    redis_pkg.asyncio = redis_asyncio
    monkeypatch.setitem(sys.modules, "redis", redis_pkg)
    monkeypatch.setitem(sys.modules, "redis.asyncio", redis_asyncio)
    captured = {}
    original = redis_cls.from_url
    def capture(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)
    redis_cls.from_url = capture
    app = create_app(settings(), clock=lambda: 1_700_000_000)
    async def run():
      async with app.router.lifespan_context(app):
        assert app.state.wecom_state is not None
    asyncio.run(run())
    assert captured["decode_responses"] is False
    assert captured["socket_connect_timeout"] == 2.0
    assert captured["socket_timeout"] == 2.0
    fake_client.aclose.assert_awaited_once()
