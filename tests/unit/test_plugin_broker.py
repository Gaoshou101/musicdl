import base64
import ipaddress
import socket

import pytest

from musicdl.contracts.plugin import EgressPolicy, HttpAction
from musicdl.plugins.broker import ActionDenied, HttpsActionBroker


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
    def __init__(self, sock):
        self.sock = sock
        self.server_hostname = None

    def wrap_socket(self, sock, server_hostname=None):
        self.server_hostname = server_hostname
        return sock


def action(url, *, bypass_contract=False, method="GET", headers=None, body=""):
    if bypass_contract:
        return HttpAction.model_construct(action_id="a1", method=method, url=url,
                                          headers=headers or {}, body=body)
    return HttpAction(action_id="a1", method=method, url=url, headers=headers or {}, body=body)


def broker(raw=b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\nhello", address="93.184.216.34"):
    sock = FakeSocket(raw)
    tls = FakeTLS(sock)
    seen = {}

    def resolve(host, port):
        seen["resolve"] = (host, port)
        return [(socket.AF_INET, (address, port))]

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
    "https://api.example.com/x#frag",
])
def test_url_policy_rejects_unsafe_forms(url):
    b, *_ = broker()
    denied(b, url, code="url_denied")


def test_an_explicit_default_port_is_the_same_target():
    b, _, _, seen = broker()
    b.fetch(action("https://api.example.com:443/x"), ("api.example.com",))
    assert seen["resolve"] == ("api.example.com", 443)


def test_a_port_outside_the_policy_is_refused_before_resolution():
    b, _, _, seen = broker()
    denied(b, "https://api.example.com:8443/x", code="port_denied")
    assert seen == {}
    b, sock, _, _ = broker()
    policy = EgressPolicy(allowed_hosts=("api.example.com",), allowed_ports=(443, 8443))
    b.fetch(action("https://api.example.com:8443/x"), policy)
    assert b"GET /x HTTP/1.1\r\n" in sock.sent
    assert b"Host: api.example.com:8443\r\n" in sock.sent


def test_plain_http_needs_the_insecure_grant():
    b, _, _, seen = broker()
    denied(b, "http://api.example.com/x", code="scheme_denied")
    assert seen == {}
    b, sock, tls, seen = broker()
    policy = EgressPolicy(allowed_hosts=("api.example.com",), allow_insecure_http=True,
                          allowed_ports=(443, 80))
    obs = b.fetch(action("http://api.example.com/x"), policy)
    assert obs.status_code == 200
    assert seen["resolve"] == ("api.example.com", 80)
    assert seen["connect"][0] == ("93.184.216.34", 80)
    # Plain HTTP never negotiates TLS, and the authority carries no default port.
    assert tls.server_hostname is None
    assert b"Host: api.example.com\r\n" in sock.sent


def test_an_address_literal_needs_the_ip_grant():
    b, _, _, seen = broker()
    denied(b, "https://103.79.184.97/x", code="host_denied")
    assert seen == {}
    b, _, _, seen = broker(address="103.79.184.97")
    policy = EgressPolicy(allowed_hosts=("103.79.184.97",), allow_ip_hosts=True)
    assert b.fetch(action("https://103.79.184.97/x"), policy).status_code == 200
    assert seen["resolve"] == ("103.79.184.97", 443)


def test_the_any_host_grant_reaches_a_host_no_analysis_could_derive():
    b, _, tls, seen = broker()
    denied(b, "https://unknown.example/x", code="host_denied")
    obs = b.fetch(action("https://unknown.example/x"), EgressPolicy(allow_any_host=True))
    assert obs.status_code == 200 and tls.server_hostname == "unknown.example"
    assert seen["resolve"][0] == "unknown.example"


def test_post_carries_the_method_headers_and_body():
    payload = base64.b64encode(b'{"id":"1"}')
    b, sock, _, _ = broker()
    b.fetch(action("https://api.example.com/x", method="POST",
                   headers={"X-Token": "secret", "Content-Type": "application/json"},
                   body=payload.decode()), ("api.example.com",))
    assert sock.sent.startswith(b"POST /x HTTP/1.1\r\n")
    assert b"X-Token: secret\r\n" in sock.sent
    assert b"Content-Type: application/json\r\n" in sock.sent
    assert b"Content-Length: 10\r\n" in sock.sent
    assert sock.sent.endswith(b"\r\n\r\n" + b'{"id":"1"}')


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


@pytest.mark.parametrize("value", ["-1", "+1", "1_0", "0x10", "", " ", "１２"])
def test_content_length_requires_ascii_decimal(value):
    raw = b"HTTP/1.1 200 OK\r\nContent-Length: " + value.encode("utf-8") + b"\r\n\r\n"
    b, *_ = broker(raw)
    denied(b, "https://api.example.com/x", code="http_error")


def test_content_length_accepts_zero_and_one_megabyte():
    for value, body in (("0", b""), ("1048576", b"x" * 1048576)):
        raw = b"HTTP/1.1 200 OK\r\nContent-Length: " + value.encode("ascii") + b"\r\n\r\n" + body
        b, *_ = broker(raw)
        obs = b.fetch(action("https://api.example.com/x"), ("api.example.com",))
        assert len(base64.b64decode(obs.body)) == len(body)


@pytest.mark.parametrize("url", ["https://api.example.com/café", "https://api.example.com/x?q=café"])
def test_non_ascii_request_target_is_rejected_before_resolution(url):
    b = HttpsActionBroker(resolver=lambda *_: pytest.fail("must not resolve"),
                          connector=lambda *_: pytest.fail("must not connect"))
    denied(b, url, code="url_denied")
