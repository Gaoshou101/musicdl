import asyncio
import ipaddress
import socket
import threading
import time

import pytest

from musicdl.contracts.plugin import ResolvedMedia
from musicdl.media.models import MediaError
from musicdl.media.transport import SecureMediaTransport


class FakeSocket:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.sent = b""
        self.closed = False
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, data):
        self.sent += data

    def makefile(self, *args, **kwargs):
        from io import BytesIO
        return BytesIO(self.raw)

    def close(self):
        self.closed = True


class FakeTLS:
    def __init__(self):
        self.server_hostname = None
        self.wrapped = None

    def wrap(self, sock, server_hostname):
        self.server_hostname = server_hostname
        self.wrapped = sock
        return sock


def media(url="https://täst.example/song.mp3", *, size=None):
    return ResolvedMedia(candidate_id="1", url=url, extension="mp3", media_type="audio/mpeg", declared_size=size)


def transport(raw=b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: 10\r\n\r\nID3payload"):
    sock = FakeSocket(raw)
    tls = FakeTLS()
    seen = {}

    def resolve(host, port):
        seen["resolve"] = (host, port)
        return [(socket.AF_INET, ("93.184.216.34", 443))]

    def connect(address, timeout):
        seen["connect"] = (address, timeout)
        return sock

    return SecureMediaTransport(resolver=resolve, connector=connect, tls_wrap=tls.wrap), sock, tls, seen


def error_code(coro, code):
    with pytest.raises(MediaError) as exc:
        asyncio.run(coro)
    assert exc.value.code == code


def test_transport_pins_idna_host_and_streams_with_fixed_request():
    transport_instance, sock, tls, seen = transport()
    metadata = asyncio.run(transport_instance.open(media(), allowed_hosts=("xn--tst-qla.example",)))

    async def read():
        return b"".join([chunk async for chunk in metadata.chunks])

    assert asyncio.run(read()) == b"ID3payload"
    asyncio.run(metadata.aclose())
    assert seen["resolve"] == ("xn--tst-qla.example", 443)
    assert seen["connect"][0] == ("93.184.216.34", 443)
    assert tls.server_hostname == "xn--tst-qla.example"
    assert sock.sent == (
        b"GET /song.mp3 HTTP/1.1\r\n"
        b"Host: xn--tst-qla.example\r\n"
        b"Accept: application/octet-stream\r\n"
        b"Accept-Encoding: identity\r\n"
        b"Connection: close\r\n\r\n"
    )
    assert sock.closed


def test_transport_rejects_non_allowlisted_host_before_dns():
    transport_instance, _, _, _ = transport()
    error_code(transport_instance.open(media(), allowed_hosts=("other.example",)), "media_host_denied")


@pytest.mark.parametrize("ip", [
    "0.0.0.0", "10.0.0.1", "100.64.0.1", "127.0.0.1", "169.254.1.1", "192.168.1.1",
    "::", "::1", "fc00::1", "fe80::1", "2001:db8::1",
])
def test_transport_rejects_every_non_global_dns_answer(ip):
    parsed = ipaddress.ip_address(ip)
    family = socket.AF_INET if parsed.version == 4 else socket.AF_INET6
    transport_instance = SecureMediaTransport(
        resolver=lambda *_: [(family, (ip, 443))],
        connector=lambda *_: pytest.fail("must not connect"),
    )
    error_code(transport_instance.open(media("https://api.example/song.mp3"), allowed_hosts=("api.example",)), "media_address_denied")


def test_transport_rejects_mixed_global_and_private_answers():
    transport_instance = SecureMediaTransport(
        resolver=lambda *_: [
            (socket.AF_INET, ("93.184.216.34", 443)),
            (socket.AF_INET, ("10.0.0.1", 443)),
        ],
        connector=lambda *_: pytest.fail("must not connect"),
    )
    error_code(transport_instance.open(media("https://api.example/song.mp3"), allowed_hosts=("api.example",)), "media_address_denied")


def test_transport_rejects_redirect_and_non_success():
    for status, code in [(301, "media_redirect_denied"), (404, "media_response_invalid")]:
        transport_instance, *_ = transport(f"HTTP/1.1 {status} Response\r\nContent-Type: audio/mpeg\r\n\r\n".encode())
        error_code(transport_instance.open(media(), allowed_hosts=("xn--tst-qla.example",)), code)


def test_transport_rejects_header_limits_and_encoding():
    cases = [
        (b"X-Header: x\r\n" * 65, "media_response_invalid"),
        (b"Content-Type: " + b"x" * 8193 + b"\r\n", "media_response_invalid"),
        (b"Content-Encoding: gzip\r\n", "media_response_invalid"),
        (b"Content-Length: nope\r\n", "media_response_invalid"),
    ]
    for header, code in cases:
        raw = b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n" + header + b"\r\nID3payload"
        transport_instance, *_ = transport(raw)
        error_code(transport_instance.open(media(), allowed_hosts=("xn--tst-qla.example",)), code)


def test_transport_rejects_content_type_and_size_mismatch():
    wrong_mime = b"HTTP/1.1 200 OK\r\nContent-Type: audio/flac\r\nContent-Length: 10\r\n\r\nID3payload"
    instance, *_ = transport(wrong_mime)
    error_code(instance.open(media(), allowed_hosts=("xn--tst-qla.example",)), "media_response_invalid")

    wrong_size = b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: 9\r\n\r\nID3payload"
    instance, *_ = transport(wrong_size)
    error_code(instance.open(media(size=10), allowed_hosts=("xn--tst-qla.example",)), "size_mismatch")


def test_transport_bounds_stream_and_closes_on_read_error():
    instance, sock, _, _ = transport(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n\r\nID3payload")
    instance.max_bytes = 3
    metadata = asyncio.run(instance.open(media(), allowed_hosts=("xn--tst-qla.example",)))

    async def read():
        return [chunk async for chunk in metadata.chunks]

    error_code(read(), "file_too_large")
    asyncio.run(metadata.aclose())
    assert sock.closed


def test_transport_constructor_limits_are_validated():
    with pytest.raises(ValueError):
        SecureMediaTransport(default_timeout_ms=0)
    with pytest.raises(ValueError):
        SecureMediaTransport(max_bytes=500 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        SecureMediaTransport(chunk_size=0)


@pytest.mark.parametrize(
    "stage,expected",
    [
        ("resolve", "media_dns_failed"),
        ("connect", "media_connect_failed"),
        ("tls", "media_tls_failed"),
        ("response", "media_response_invalid"),
    ],
)
def test_transport_maps_unexpected_boundary_errors_to_stable_codes(stage, expected):
    raw = FakeSocket(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n\r\nID3")

    def resolve(*_):
        if stage == "resolve":
            raise RuntimeError("secret resolver detail")
        return [(socket.AF_INET, ("93.184.216.34", 443))]

    def connect(*_):
        if stage == "connect":
            raise RuntimeError("secret connect detail")
        return raw

    def tls(sock, _):
        if stage == "tls":
            raise RuntimeError("secret tls detail")
        return sock

    def response(_):
        if stage == "response":
            raise RuntimeError("secret response detail")
        return http_response_from_socket(raw)

    instance = SecureMediaTransport(
        resolver=resolve,
        connector=connect,
        tls_wrap=tls,
        response_factory=response,
    )
    with pytest.raises(MediaError) as caught:
        asyncio.run(instance.open(media(), allowed_hosts=("xn--tst-qla.example",)))
    assert caught.value.code == expected
    assert "secret" not in repr(caught.value)


def http_response_from_socket(sock):
    response = http.client.HTTPResponse(sock)
    response.begin()
    return response


def test_transport_runs_response_header_parsing_off_event_loop(monkeypatch):
    instance, *_ = transport()
    original = instance._response_headers
    main_thread = threading.get_ident()
    observed = []

    def wrapped(response):
        observed.append(threading.get_ident())
        return original(response)

    monkeypatch.setattr(instance, "_response_headers", wrapped)
    metadata = asyncio.run(instance.open(media(), allowed_hosts=("xn--tst-qla.example",)))
    asyncio.run(metadata.aclose())
    assert observed and observed[0] != main_thread


@pytest.mark.parametrize("stage", ["connect", "tls", "response"])
def test_transport_closes_handles_returned_after_open_cancellation(stage):
    release = threading.Event()
    started = threading.Event()
    raw = FakeSocket(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n\r\nID3")
    wrapped = FakeSocket(b"")
    late_response = type("LateResponse", (), {"close": lambda self: setattr(self, "closed", True)})()
    late_response.closed = False

    def delayed(value):
        started.set()
        release.wait(timeout=2)
        return value

    def resolve(*_):
        return [(socket.AF_INET, ("93.184.216.34", 443))]

    def connect(*_):
        if stage == "connect":
            return delayed(raw)
        return raw

    def tls(sock, _):
        if stage == "tls":
            return delayed(wrapped)
        return sock

    def response(sock):
        if stage == "response":
            return delayed(late_response)
        return http_response_from_socket(sock)

    instance = SecureMediaTransport(
        resolver=resolve,
        connector=connect,
        tls_wrap=tls,
        response_factory=response,
    )

    async def run():
        task = asyncio.create_task(instance.open(media(), allowed_hosts=("xn--tst-qla.example",)))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        task.cancel()
        threading.Timer(0.01, release.set).start()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert raw.closed
    if stage == "tls":
        assert wrapped.closed
    if stage == "response":
        assert late_response.closed
