import asyncio
import ipaddress
import socket
import threading
import time

import pytest

from musicdl.contracts.plugin import EgressPolicy, ResolvedMedia
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
    metadata = asyncio.run(transport_instance.open(media(), policy=("xn--tst-qla.example",)))

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
    error_code(transport_instance.open(media(), policy=("other.example",)), "media_host_denied")


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
    error_code(transport_instance.open(media("https://api.example/song.mp3"), policy=("api.example",)), "media_address_denied")


def test_transport_rejects_mixed_global_and_private_answers():
    transport_instance = SecureMediaTransport(
        resolver=lambda *_: [
            (socket.AF_INET, ("93.184.216.34", 443)),
            (socket.AF_INET, ("10.0.0.1", 443)),
        ],
        connector=lambda *_: pytest.fail("must not connect"),
    )
    error_code(transport_instance.open(media("https://api.example/song.mp3"), policy=("api.example",)), "media_address_denied")


def test_transport_rejects_redirect_and_non_success():
    for status, code in [(301, "media_redirect_denied"), (404, "media_response_invalid")]:
        transport_instance, *_ = transport(f"HTTP/1.1 {status} Response\r\nContent-Type: audio/mpeg\r\n\r\n".encode())
        error_code(transport_instance.open(media(), policy=("xn--tst-qla.example",)), code)


MEDIA_OK = b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: 10\r\n\r\nID3payload"


def redirect_transport(responses, *, addresses=None, **kwargs):
    """Serve one scripted response per hop and record what each hop reached."""
    sockets = [FakeSocket(raw) for raw in responses]
    connects = []
    hostnames = []
    resolved = []
    address_map = dict(addresses or {})

    def resolve(host, port):
        resolved.append((host, port))
        return [(socket.AF_INET, (address_map.get(host, "93.184.216.34"), port))]

    def connect(address, timeout):
        connects.append(address)
        if len(connects) > len(sockets):
            raise AssertionError("the transport opened more hops than the test scripted")
        return sockets[len(connects) - 1]

    def tls(sock, server_hostname):
        hostnames.append(server_hostname)
        return sock

    instance = SecureMediaTransport(resolver=resolve, connector=connect, tls_wrap=tls, **kwargs)
    return instance, sockets, connects, hostnames, resolved


def test_transport_follows_a_redirect_to_a_policy_approved_host():
    redirect = b"HTTP/1.1 302 Found\r\nLocation: https://cdn.example/song.mp3\r\nContent-Length: 0\r\n\r\n"
    instance, sockets, _, hostnames, resolved = redirect_transport([redirect, MEDIA_OK])
    policy = EgressPolicy(allowed_hosts=("xn--tst-qla.example", "cdn.example"))

    metadata = asyncio.run(instance.open(media(), policy=policy))

    async def read():
        return b"".join([chunk async for chunk in metadata.chunks])

    assert asyncio.run(read()) == b"ID3payload"
    asyncio.run(metadata.aclose())
    assert resolved == [("xn--tst-qla.example", 443), ("cdn.example", 443)]
    assert hostnames == ["xn--tst-qla.example", "cdn.example"]
    assert sockets[0].closed and sockets[1].closed
    assert sockets[1].sent == (
        b"GET /song.mp3 HTTP/1.1\r\nHost: cdn.example\r\n"
        b"Accept: application/octet-stream\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n"
    )


def test_transport_follows_a_relative_redirect_on_the_same_host():
    instance, sockets, _, _, resolved = redirect_transport([
        b"HTTP/1.1 301 Moved Permanently\r\nLocation: /real/song.mp3\r\n\r\n", MEDIA_OK])

    metadata = asyncio.run(instance.open(media("https://täst.example/old.mp3"),
                                         policy=("xn--tst-qla.example",)))

    asyncio.run(metadata.aclose())
    assert resolved == [("xn--tst-qla.example", 443)] * 2
    assert sockets[1].sent.startswith(b"GET /real/song.mp3 HTTP/1.1\r\nHost: xn--tst-qla.example\r\n")


def test_transport_refuses_a_redirect_past_the_policy():
    redirect = b"HTTP/1.1 302 Found\r\nLocation: https://elsewhere.example/song.mp3\r\n\r\n"
    instance, sockets, connects, _, _ = redirect_transport([redirect, MEDIA_OK])

    error_code(instance.open(media(), policy=("xn--tst-qla.example",)), "media_host_denied")

    assert connects == [("93.184.216.34", 443)]
    assert sockets[0].closed and sockets[1].sent == b""


def test_transport_refuses_a_redirect_to_a_private_address():
    redirect = b"HTTP/1.1 302 Found\r\nLocation: https://internal.example/song.mp3\r\n\r\n"
    instance, sockets, *_ = redirect_transport([redirect], addresses={"internal.example": "10.0.0.1"})

    error_code(instance.open(media(), policy=EgressPolicy(allow_any_host=True)), "media_address_denied")

    assert sockets[0].closed


def test_transport_stops_a_redirect_loop_at_the_budget():
    loop = b"HTTP/1.1 302 Found\r\nLocation: https://xn--tst-qla.example/song.mp3\r\n\r\n"
    instance, sockets, connects, _, _ = redirect_transport([loop] * 8, max_redirects=2)

    error_code(instance.open(media(), policy=("xn--tst-qla.example",)), "media_redirect_denied")

    assert len(connects) == 3
    assert all(sock.closed for sock in sockets[:3]) and sockets[3].sent == b""


def test_transport_refuses_the_first_redirect_when_the_budget_is_zero():
    redirect = b"HTTP/1.1 302 Found\r\nLocation: https://cdn.example/song.mp3\r\n\r\n"
    instance, sockets, _, _, _ = redirect_transport([redirect, MEDIA_OK], max_redirects=0)

    error_code(instance.open(media(), policy=EgressPolicy(allow_any_host=True)), "media_redirect_denied")

    assert sockets[0].closed and sockets[1].sent == b""


def test_transport_refuses_a_redirect_that_downgrades_without_the_grant():
    redirect = b"HTTP/1.1 302 Found\r\nLocation: http://cdn.example/song.mp3\r\n\r\n"
    hosts = ("xn--tst-qla.example", "cdn.example")

    instance, sockets, _, _, _ = redirect_transport([redirect, MEDIA_OK], max_redirects=1)
    error_code(instance.open(media(), policy=hosts), "media_url_denied")
    assert sockets[0].closed and sockets[1].sent == b""

    instance, sockets, _, _, resolved = redirect_transport([redirect, MEDIA_OK], max_redirects=1)
    policy = EgressPolicy(allowed_hosts=hosts, allow_insecure_http=True)
    metadata = asyncio.run(instance.open(media(), policy=policy))

    asyncio.run(metadata.aclose())
    assert resolved == [("xn--tst-qla.example", 443), ("cdn.example", 80)]


def test_transport_spends_one_deadline_across_every_redirect_hop():
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    redirect = b"HTTP/1.1 302 Found\r\nLocation: https://cdn.example/song.mp3\r\n\r\n"
    sockets = [FakeSocket(redirect), FakeSocket(MEDIA_OK)]
    connects = []

    def connect(address, timeout):
        connects.append(address)
        if len(connects) > 1:
            clock.value = 100.0
        return sockets[len(connects) - 1]

    instance = SecureMediaTransport(
        resolver=lambda *_: [(socket.AF_INET, ("93.184.216.34", 443))],
        connector=connect,
        tls_wrap=lambda sock, _: sock,
        clock=clock,
    )
    error_code(instance.open(media(), policy=EgressPolicy(allow_any_host=True)), "media_timeout")

    assert len(connects) == 2
    assert sockets[0].closed and sockets[1].closed


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
        error_code(transport_instance.open(media(), policy=("xn--tst-qla.example",)), code)


def test_transport_rejects_content_type_and_size_mismatch():
    wrong_mime = b"HTTP/1.1 200 OK\r\nContent-Type: audio/flac\r\nContent-Length: 10\r\n\r\nID3payload"
    instance, *_ = transport(wrong_mime)
    error_code(instance.open(media(), policy=("xn--tst-qla.example",)), "media_response_invalid")

    wrong_size = b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: 9\r\n\r\nID3payload"
    instance, *_ = transport(wrong_size)
    error_code(instance.open(media(size=10), policy=("xn--tst-qla.example",)), "size_mismatch")


def test_transport_accepts_the_vendor_alias_of_the_container_it_expected():
    # The CDN a resolved keyword points at answers `audio/x-flac`; refusing that
    # alias refused a real song, so the alias is part of the contract.
    raw = b"HTTP/1.1 200 OK\r\nContent-Type: audio/x-flac\r\nContent-Length: 4\r\n\r\nfLaC"
    instance, *_ = transport(raw)
    flac = ResolvedMedia(candidate_id="1", url="https://täst.example/song.flac",
                         extension="flac", media_type="audio/flac", declared_size=None)

    metadata = asyncio.run(instance.open(flac, policy=("xn--tst-qla.example",)))

    asyncio.run(metadata.aclose())
    assert metadata.extension == "flac" and metadata.media_type == "audio/flac"


def test_transport_lets_the_bytes_override_a_wrong_content_type():
    # Measured 2026-09-17 on iot202.music.126.net: the FLAC stream a resolved
    # keyword points at arrives with `Content-Type: audio/mpeg`, and trusting
    # the label refused a real song.  The bytes are `fLaC`, so they decide.
    raw = b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: 8\r\n\r\nfLaClaC!"
    instance, *_ = transport(raw)
    flac = ResolvedMedia(candidate_id="1", url="https://täst.example/song.flac",
                         extension="flac", media_type="audio/flac", declared_size=None)

    metadata = asyncio.run(instance.open(flac, policy=("xn--tst-qla.example",)))

    async def read():
        return b"".join([chunk async for chunk in metadata.chunks])

    # The bytes read to classify the body are the head of the download.
    assert asyncio.run(read()) == b"fLaClaC!"
    asyncio.run(metadata.aclose())


def test_transport_refuses_bytes_that_contradict_the_extension_too():
    # The same override must not become a licence to accept anything: a body
    # that names a different container than the resolved extension is still a
    # refusal, and it is the refusal `validate_media` would have raised later.
    raw = b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: 10\r\n\r\nID3payload"
    instance, *_ = transport(raw)
    flac = ResolvedMedia(candidate_id="1", url="https://täst.example/song.flac",
                         extension="flac", media_type="audio/flac", declared_size=None)

    error_code(instance.open(flac, policy=("xn--tst-qla.example",)), "media_response_invalid")


def _octet_stream_raw(body: bytes) -> bytes:
    return (b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: "
            + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)


def _m4a_media():
    return ResolvedMedia(candidate_id="1", url="https://täst.example/song.m4a",
                         extension="m4a", media_type="audio/mp4", declared_size=None)


def test_transport_reads_the_mp4_box_whose_brand_names_the_container():
    # Measured 2026-09-17 on car-bj.kuwo.cn: kuwo's car CDN answers its `.aac`
    # links with `Content-Type: application/octet-stream` and an ftyp/mp42 box
    # whose `M4A ` brand sits at byte 16 -- past the 16-byte prefix that used to
    # decide, which is why four sources' downloads were refused.
    body = bytes.fromhex("00000020667479706d703432000000004d3441206d70343269736f6d00000000") + b"moov"
    instance, *_ = transport(_octet_stream_raw(body))

    metadata = asyncio.run(instance.open(_m4a_media(), policy=("xn--tst-qla.example",)))

    async def read():
        return b"".join([chunk async for chunk in metadata.chunks])

    assert asyncio.run(read()) == body
    asyncio.run(metadata.aclose())


def test_transport_finishes_an_mp4_box_longer_than_the_prefix():
    # The box states its own length, so a brand list longer than the prefix is
    # read out instead of being truncated into a refusal.
    brands = b"M4A " + b"isom" * 24
    size = 16 + len(brands)
    body = size.to_bytes(4, "big") + b"ftypmp42\x00\x00\x00\x00" + brands + b"moov"
    instance, *_ = transport(_octet_stream_raw(body))

    metadata = asyncio.run(instance.open(_m4a_media(), policy=("xn--tst-qla.example",)))

    async def read():
        return b"".join([chunk async for chunk in metadata.chunks])

    assert asyncio.run(read()) == body
    asyncio.run(metadata.aclose())


def test_transport_bounds_stream_and_closes_on_read_error():
    instance, sock, _, _ = transport(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n\r\nID3payload")
    instance.max_bytes = 3
    metadata = asyncio.run(instance.open(media(), policy=("xn--tst-qla.example",)))

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
        asyncio.run(instance.open(media(), policy=("xn--tst-qla.example",)))
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
    metadata = asyncio.run(instance.open(media(), policy=("xn--tst-qla.example",)))
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
        task = asyncio.create_task(instance.open(media(), policy=("xn--tst-qla.example",)))
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


def test_transport_refuses_a_port_the_policy_does_not_allow():
    instance, *_ = transport()
    error_code(instance.open(media("https://api.example:8443/song.mp3"), policy=("api.example",)), "media_url_denied")


def test_transport_reaches_the_socket_a_granted_port_names():
    instance, sock, tls, seen = transport()
    policy = EgressPolicy(allowed_hosts=("api.example",), allowed_ports=(443, 8443))
    metadata = asyncio.run(instance.open(media("https://api.example:8443/song.mp3"), policy=policy))

    asyncio.run(metadata.aclose())
    assert seen["resolve"] == ("api.example", 8443)
    assert tls.server_hostname == "api.example"
    assert sock.sent.startswith(b"GET /song.mp3 HTTP/1.1\r\nHost: api.example:8443\r\n")


def test_transport_allows_plain_http_only_with_the_grant():
    instance, *_ = transport()
    error_code(instance.open(media("http://api.example/song.mp3"), policy=("api.example",)), "media_url_denied")

    instance, sock, tls, seen = transport()
    policy = EgressPolicy(allowed_hosts=("api.example",), allow_insecure_http=True)
    metadata = asyncio.run(instance.open(media("http://api.example/song.mp3"), policy=policy))

    asyncio.run(metadata.aclose())
    assert seen["resolve"] == ("api.example", 80)
    assert tls.server_hostname is None
    assert sock.sent.startswith(b"GET /song.mp3 HTTP/1.1\r\nHost: api.example\r\n")


def test_transport_reaches_any_host_only_with_the_grant():
    instance, *_ = transport()
    error_code(instance.open(media("https://cdn.unknown.example/song.mp3"), policy=("api.example",)), "media_host_denied")

    instance, sock, tls, seen = transport()
    metadata = asyncio.run(instance.open(media("https://cdn.unknown.example/song.mp3"),
                                         policy=EgressPolicy(allow_any_host=True)))

    asyncio.run(metadata.aclose())
    assert seen["resolve"] == ("cdn.unknown.example", 443)
    assert tls.server_hostname == "cdn.unknown.example"


def test_transport_refuses_an_ip_literal_without_the_grant():
    instance, *_ = transport()
    error_code(instance.open(media("https://103.79.184.97/song.mp3"), policy=("103.79.184.97",)), "media_host_denied")

    instance, sock, tls, seen = transport()
    policy = EgressPolicy(allowed_hosts=("103.79.184.97",), allow_ip_hosts=True)
    metadata = asyncio.run(instance.open(media("https://103.79.184.97/song.mp3"), policy=policy))

    asyncio.run(metadata.aclose())
    assert seen["resolve"] == ("103.79.184.97", 443)
