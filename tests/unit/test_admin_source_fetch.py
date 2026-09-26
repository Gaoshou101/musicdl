import asyncio
import base64
import socket
import threading
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from musicdl.admin.auth import AdminAuth
from musicdl.admin.health import EventLogStore
from musicdl.admin.portal import create_admin_router
from musicdl.admin.source_fetch import SourceFetchError, _guarded_resolver, fetch_source
from musicdl.plugins.broker import ActionDenied, HttpsActionBroker
from musicdl.plugins.store import PluginStore


def observation(source: bytes, *, status_code: int = 200, content_type: str = "text/plain"):
    return SimpleNamespace(status_code=status_code,
                           headers={"content-type": content_type},
                           body=base64.b64encode(source).decode("ascii"))


class FakeBroker:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def fetch(self, action, policy, *, timeout):
        self.calls.append((action, policy, timeout))
        return self.result


class FakeSocket:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.sent = b""
        self.closed = False

    def settimeout(self, _value):
        return None

    def sendall(self, data):
        self.sent += data

    def makefile(self, *_args, **_kwargs):
        return BytesIO(self.raw)

    def close(self):
        self.closed = True


class FakeTLS:
    def wrap_socket(self, sock, server_hostname=None):
        self.server_hostname = server_hostname
        return sock


def pinned_broker(raw: bytes, *, seen: dict | None = None):
    sock = FakeSocket(raw)
    tls = FakeTLS()

    def resolve(host, port):
        if seen is not None:
            seen["resolve"] = (host, port)
        return [(socket.AF_INET, ("93.184.216.34", port))]

    def connect(address, timeout):
        if seen is not None:
            seen["connect"] = (address, timeout)
        return sock

    return HttpsActionBroker(resolver=resolve, connector=connect, ssl_context=tls), sock, tls


def test_fetch_source_decodes_bom_and_derives_only_the_path_filename():
    broker = FakeBroker(observation(b"\xef\xbb\xbfprint('ok')\n"))

    result = fetch_source("https://example.com/dir/%E6%98%9F.py?filename=secret.js", broker=broker)

    assert result == {"script": "print('ok')\n", "filename": "星.py", "language": "python"}
    action, policy, timeout = broker.calls[0]
    assert action.method == "GET"
    assert action.headers == {} and action.body == ""
    assert policy.allowed_hosts == ("example.com",)
    assert policy.allowed_ports == (443,) and policy.allow_insecure_http is False
    assert timeout == 10.0


@pytest.mark.parametrize("url", [
    "ftp://example.com/source.js",
    "https://127.0.0.1/source.js",
    "https://user:password@example.com/source.js",
    "https://example.com:8443/source.js",
    "https://example.com/source.js#fragment",
    "https://example.com/source.js\nsecret",
])
def test_fetch_source_rejects_unsafe_urls_before_broker(url):
    broker = FakeBroker(observation(b"print('must not run')"))

    with pytest.raises(SourceFetchError) as exc:
        fetch_source(url, broker=broker)

    assert exc.value.code in {"url_denied", "port_denied", "host_denied"}
    assert broker.calls == []


@pytest.mark.parametrize(("source", "content_type", "code"), [
    (b"", "text/plain", "source_empty"),
    (b"\x00print('no')", "text/plain", "source_nul"),
    (b"\xff\xfe", "text/plain", "source_encoding"),
    (b"<html><body>login</body></html>", "text/html", "source_html"),
])
def test_fetch_source_rejects_non_script_content(source, content_type, code):
    broker = FakeBroker(observation(source, content_type=content_type))

    with pytest.raises(SourceFetchError) as exc:
        fetch_source("https://example.com/source.js", broker=broker)

    assert exc.value.code == code


def test_fetch_source_enforces_the_source_size_ceiling_before_decoding():
    broker = FakeBroker(observation(b"x" * (256 * 1024 + 1)))

    with pytest.raises(SourceFetchError) as exc:
        fetch_source("https://example.com/source.js", broker=broker)

    assert exc.value.code == "source_too_large"


def test_fetch_source_uses_an_exact_host_pinned_broker():
    seen = {}
    broker, sock, tls = pinned_broker(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nprint('ok')",
        seen=seen,
    )

    result = fetch_source("https://example.com/source.py?token=not-a-filename", broker=broker)

    assert result == {"script": "print('ok')", "filename": "source.py", "language": "python"}
    assert seen["resolve"] == ("example.com", 443)
    assert seen["connect"][0] == ("93.184.216.34", 443)
    assert tls.server_hostname == "example.com"
    assert b"Host: example.com\r\n" in sock.sent
    assert b"token=not-a-filename" in sock.sent
    assert sock.closed


@pytest.mark.parametrize(("raw", "code"), [
    (b"HTTP/1.1 302 Found\r\nLocation: https://other.example/\r\n\r\n", "redirect_denied"),
    (b"HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\n\r\nmissing", "http_error"),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\n\r\n", "body_too_large"),
])
def test_fetch_source_maps_pinned_transport_failures_to_stable_codes(raw, code):
    broker, *_ = pinned_broker(raw)

    with pytest.raises(SourceFetchError) as exc:
        fetch_source("https://example.com/source.js", broker=broker)

    assert exc.value.code == code


def test_guarded_resolver_rejects_one_private_answer_in_a_mixed_result():
    resolver = _guarded_resolver(lambda *_: [
        (socket.AF_INET, ("93.184.216.34", 443)),
        (socket.AF_INET, ("10.0.0.7", 443)),
    ])

    with pytest.raises(Exception) as exc:
        resolver("example.com", 443)

    assert getattr(exc.value, "code", None) == "address_denied"


@pytest.mark.parametrize("ip", [
    "10.0.0.7",      # private
    "224.0.0.1",     # multicast
    "240.0.0.1",     # reserved
    "192.0.2.1",     # documentation/reserved
    "ff02::1",       # IPv6 multicast
    "2001:db8::1",   # IPv6 documentation/reserved
])
def test_guarded_resolver_rejects_private_multicast_and_reserved_answers(ip):
    import ipaddress

    parsed = ipaddress.ip_address(ip)
    family = socket.AF_INET if parsed.version == 4 else socket.AF_INET6
    resolver = _guarded_resolver(lambda *_: [(family, (ip, 443))])

    with pytest.raises(ActionDenied) as exc:
        resolver("example.com", 443)

    assert exc.value.code == "address_denied"


def _client(tmp_path, *, source_fetcher, audit):
    app = FastAPI()
    auth = AdminAuth()
    auth.change_credentials("admin", "operator", "new-password")
    app.include_router(create_admin_router(auth=auth, audit=audit, source_fetcher=source_fetcher,
                                           plugins=lambda: PluginStore(tmp_path)))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


async def _login(client):
    response = await client.post("/admin/login", json={"username": "operator", "password": "new-password"})
    return response.json()["csrf_token"]


def test_fetch_route_is_csrf_bound_and_has_no_storage_or_audit_side_effect(tmp_path):
    calls = []
    audit = EventLogStore()

    def fetcher(url):
        calls.append(url)
        return {"script": "print('ok')\n", "filename": "source.py", "language": "python"}

    async def run():
        async with _client(tmp_path, source_fetcher=fetcher, audit=audit) as client:
            forged = await client.post("/admin/sources/fetch", json={"url": "https://example.com/source.py"})
            csrf = await _login(client)
            missing_csrf = await client.post("/admin/sources/fetch", json={"url": "https://example.com/source.py"})
            fetched = await client.post("/admin/sources/fetch", headers={"x-csrf-token": csrf},
                                        json={"url": "https://example.com/source.py"})
            sources = await client.get("/admin/sources")
        return forged, missing_csrf, fetched, sources

    forged, missing_csrf, fetched, sources = asyncio.run(run())

    assert forged.status_code == 401
    assert missing_csrf.status_code == 403
    assert fetched.status_code == 200
    assert fetched.json() == {"script": "print('ok')\n", "filename": "source.py", "language": "python"}
    assert sources.json()["items"] == []
    assert calls == ["https://example.com/source.py"]
    assert all(item.get("action") != "fetch_source" for item in audit.page()["items"])


def test_fetch_route_enforces_the_credential_change_gate_before_fetch(tmp_path):
    calls = []
    auth = AdminAuth()
    audit = EventLogStore()

    def fetcher(url):
        calls.append(url)
        return {"script": "print('must not run')", "filename": "source.py", "language": "python"}

    app = FastAPI()
    app.include_router(create_admin_router(auth=auth, audit=audit, source_fetcher=fetcher,
                                           plugins=lambda: PluginStore(tmp_path)))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
            login = await client.post("/admin/login", json={"username": "admin", "password": "password"})
            csrf = login.json()["csrf_token"]
            gated = await client.post("/admin/sources/fetch", headers={"x-csrf-token": csrf},
                                      json={"url": "https://example.com/source.py"})
        return login, gated

    login, gated = asyncio.run(run())

    assert login.status_code == 200 and login.json()["must_change"] is True
    assert gated.status_code == 403 and gated.json() == {"detail": "credential change required"}
    assert calls == []


def test_fetch_route_timeout_keeps_all_worker_slots_until_workers_return(tmp_path, monkeypatch):
    monkeypatch.setattr("musicdl.admin.portal.SOURCE_FETCH_DEADLINE", 0.05)
    blocked_urls = [f"https://example.com/blocked-{index}.py" for index in range(4)]
    fifth_url = "https://example.com/fifth.py"
    next_url = "https://example.com/next.py"
    calls = []
    calls_lock = threading.Lock()
    started = {url: threading.Event() for url in blocked_urls}
    release = threading.Event()
    workers_returned = threading.Event()
    returned_count = 0

    def fetcher(url):
        nonlocal returned_count
        with calls_lock:
            calls.append(url)
        if url in started:
            started[url].set()
            release.wait(2)
            with calls_lock:
                returned_count += 1
                if returned_count == len(blocked_urls):
                    workers_returned.set()
        return {"script": "print('ok')", "filename": "source.py", "language": "python"}

    async def run():
        async with _client(tmp_path, source_fetcher=fetcher, audit=EventLogStore()) as client:
            csrf = await _login(client)
            tasks = [asyncio.create_task(client.post(
                "/admin/sources/fetch", headers={"x-csrf-token": csrf}, json={"url": url}))
                     for url in blocked_urls]
            try:
                for event in started.values():
                    assert await asyncio.to_thread(event.wait, 1)
                first_responses = await asyncio.gather(*tasks)
                fifth = await client.post("/admin/sources/fetch", headers={"x-csrf-token": csrf},
                                          json={"url": fifth_url})
                with calls_lock:
                    before_release = list(calls)
                assert all(response.status_code == 504 for response in first_responses)
                assert fifth.status_code == 504 and fifth.json() == {"detail": "timeout"}
                assert sorted(before_release) == sorted(blocked_urls)

                release.set()
                assert await asyncio.to_thread(workers_returned.wait, 1)
                # Give each done callback a turn to return its semaphore permit;
                # the relaxed deadline below makes this assertion independent of
                # callback scheduling jitter while still failing on a leaked slot.
                await asyncio.sleep(0)
                monkeypatch.setattr("musicdl.admin.portal.SOURCE_FETCH_DEADLINE", 1.0)
                next_response = await client.post("/admin/sources/fetch",
                                                  headers={"x-csrf-token": csrf}, json={"url": next_url})
                with calls_lock:
                    after_release = list(calls)
            finally:
                release.set()
                await asyncio.to_thread(workers_returned.wait, 1)
        return first_responses, fifth, next_response, before_release, after_release

    first_responses, fifth, next_response, before_release, after_release = asyncio.run(run())

    assert all(response.status_code == 504 and response.json() == {"detail": "timeout"}
               for response in first_responses)
    assert fifth.status_code == 504 and fifth.json() == {"detail": "timeout"}
    assert sorted(before_release) == sorted(blocked_urls)
    assert next_response.status_code == 200
    assert after_release.count(next_url) == 1
    assert fifth_url not in after_release


@pytest.mark.parametrize(("failure", "status", "detail"), [
    (SourceFetchError("source_html"), 422, "source_html"),
    (RuntimeError("https://secret.example/?token=do-not-leak"), 502, "fetch_failed"),
])
def test_fetch_route_returns_stable_error_codes_without_exception_text(tmp_path, failure, status, detail):
    def fetcher(_url):
        raise failure

    async def run():
        async with _client(tmp_path, source_fetcher=fetcher, audit=EventLogStore()) as client:
            csrf = await _login(client)
            return await client.post("/admin/sources/fetch", headers={"x-csrf-token": csrf},
                                     json={"url": "https://example.com/source.js"})

    response = asyncio.run(run())

    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert "secret.example" not in response.text and "do-not-leak" not in response.text


def test_fetch_route_rejects_invalid_body_before_fetching(tmp_path):
    calls = []

    def fetcher(url):
        calls.append(url)
        return {"script": "x", "filename": "source.js", "language": "javascript"}

    async def run():
        async with _client(tmp_path, source_fetcher=fetcher, audit=EventLogStore()) as client:
            csrf = await _login(client)
            malformed = await client.post("/admin/sources/fetch", headers={"x-csrf-token": csrf}, content="[")
            missing = await client.post("/admin/sources/fetch", headers={"x-csrf-token": csrf}, json={})
        return malformed, missing

    malformed, missing = asyncio.run(run())

    assert malformed.status_code == 422 and malformed.json() == {"detail": "invalid_url"}
    assert missing.status_code == 422 and missing.json() == {"detail": "invalid_url"}
    assert calls == []
