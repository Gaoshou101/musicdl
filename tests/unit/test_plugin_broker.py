import base64
import ipaddress
import socket

import pytest

from musicdl.contracts.plugin import HttpAction
from musicdl.plugins.broker import ActionDenied, HttpsActionBroker


class FakeSocket:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.sent = b""
        self.closed = False

    def sendall(self, data):
        self.sent += data

    def makefile(self, *args, **kwargs):
        from io import BytesIO
        return BytesIO(self.raw)

    def close(self):
        self.closed = True


class FakeTLS:
    def __init__(self, sock):
        self.sock = sock
        self.server_hostname = None

    def wrap_socket(self, sock, server_hostname=None):
        self.server_hostname = server_hostname
        return sock


def action(url, *, bypass_contract=False):
    if bypass_contract:
        return HttpAction.model_construct(action_id="a1", method="GET", url=url)
    return HttpAction(action_id="a1", method="GET", url=url)


def broker(raw=b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\nhello"):
    sock = FakeSocket(raw)
    tls = FakeTLS(sock)
    seen = {}

    def resolve(host, port):
        seen["resolve"] = (host, port)
        return [(socket.AF_INET, ("93.184.216.34", 443))]

    def connect(address, timeout):
        seen["connect"] = (address, timeout)
        return sock

    b = HttpsActionBroker(resolver=resolve, connector=connect, ssl_context=tls)
    return b, sock, tls, seen


def denied(b, url, hosts=("api.example.com",), code=None):
    with pytest.raises(ActionDenied) as exc:
        b.fetch(action(url, bypass_contract=True), hosts)
    if code:
        assert exc.value.code == code


def test_valid_path_query_is_pinned_and_observed():
    b, sock, tls, seen = broker()
    obs = b.fetch(action("https://api.example.com/search?q=x"), ("api.example.com",))
    assert obs.status_code == 200
    assert base64.b64decode(obs.body) == b"hello"
    assert seen["connect"][0] == ("93.184.216.34", 443)
    assert seen["resolve"] == ("api.example.com", 443)
    assert tls.server_hostname == "api.example.com"
    assert b"Host: api.example.com\r\n" in sock.sent
    assert sock.closed


def test_idna_host_is_normalized_for_allowlist_and_tls():
    b, _, tls, seen = broker()
    b.fetch(action("https://täst.example/search"), ("xn--tst-qla.example",))
    assert seen["resolve"][0] == "xn--tst-qla.example"
    assert tls.server_hostname == "xn--tst-qla.example"


@pytest.mark.parametrize("url", [
    "https://u:p@api.example.com/x",
    "https://api.example.com:443/x",
    "https://api.example.com:8443/x",
    "https://api.example.com/x#frag",
    "http://api.example.com/x",
])
def test_url_policy_rejects_unsafe_forms(url):
    b, *_ = broker()
    denied(b, url, code="url_denied")


def test_exact_host_and_proxy_environment_are_ignored(monkeypatch):
    b, *_ = broker()
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:8080")
    denied(b, "https://other.example.com/x", code="host_denied")


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_rejected(status):
    b, *_ = broker(f"HTTP/1.1 {status} Found\r\nLocation: https://other.example.com\r\n\r\n".encode())
    denied(b, "https://api.example.com/x", code="redirect_denied")


def test_content_length_and_streamed_body_are_bounded():
    b, *_ = broker(b"HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\n\r\n")
    denied(b, "https://api.example.com/x", code="body_too_large")
    b, *_ = broker(b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 1048577)
    denied(b, "https://api.example.com/x", code="body_too_large")


@pytest.mark.parametrize("ip", [
    "0.0.0.0", "10.0.0.1", "100.64.0.1", "127.0.0.1", "169.254.1.1", "192.168.1.1",
    "::", "::1", "fc00::1", "fe80::1", "2001:db8::1",
])
def test_every_non_global_candidate_is_rejected(ip):
    def resolve(host, port):
        parsed = ipaddress.ip_address(ip)
        family = socket.AF_INET if parsed.version == 4 else socket.AF_INET6
        return [(family, (ip, 443))]
    b = HttpsActionBroker(resolver=resolve, connector=lambda *_: pytest.fail("must not connect"))
    denied(b, "https://api.example.com/x", code="address_denied")


def test_mixed_global_and_private_candidates_are_rejected():
    b = HttpsActionBroker(
        resolver=lambda *_: [(socket.AF_INET, ("93.184.216.34", 443)), (socket.AF_INET, ("10.0.0.1", 443))],
        connector=lambda *_: pytest.fail("must not connect"),
    )
    denied(b, "https://api.example.com/x", code="address_denied")


def test_malformed_http_is_normalized():
    b, *_ = broker(b"not HTTP")
    denied(b, "https://api.example.com/x", code="http_error")


def test_headers_are_fixed_subset_and_bounded():
    raw = (b"HTTP/1.1 200 OK\r\nX-Large: " + b"x" * 10000 +
           b"\r\nContent-Type: application/json\r\nETag: abc\r\n\r\nhello")
    b, *_ = broker(raw)
    obs = b.fetch(action("https://api.example.com/x"), ("api.example.com",))
    assert obs.headers == {"content-type": "application/json", "etag": "abc"}


def test_unsafe_or_duplicate_selected_header_is_rejected():
    for headers in (
        b"Content-Type: " + b"x" * 1025 + b"\r\n",
        b"ETag: one\r\nETag: two\r\n",
    ):
        b, *_ = broker(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\nhello")
        denied(b, "https://api.example.com/x", code="http_error")


@pytest.mark.parametrize("entry", [
    (socket.AF_INET, ("2001:db8::1", 443)),
    (socket.AF_INET6, ("93.184.216.34", 443)),
    (999, ("93.184.216.34", 443)),
    (socket.AF_INET, ("not-an-ip", 443)),
    (socket.AF_INET, ("93.184.216.34",)),
])
def test_resolver_family_and_shape_are_validated(entry):
    b = HttpsActionBroker(resolver=lambda *_: [entry], connector=lambda *_: pytest.fail("must not connect"))
    denied(b, "https://api.example.com/x", code="address_denied")


@pytest.mark.parametrize("target", ["/bad path", "/bad\tpath", "/bad\npath"])
def test_request_target_whitespace_and_controls_are_rejected(target):
    b, sock, *_ = broker()
    denied(b, "https://api.example.com" + target, code="url_denied")
    assert sock.sent == b""
