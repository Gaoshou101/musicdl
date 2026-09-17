"""Main-owned, DNS-pinned HTTP transport for plugin HTTP actions."""

from __future__ import annotations

import base64
import http.client
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit
from typing import Callable, Iterable

from musicdl.contracts.plugin import (
    DEFAULT_EGRESS_PORT, EgressPolicy, HttpAction, HttpObservation, MAX_ACTION_BODY_BYTES,
    PluginManifest, literal_address,
)


class ActionDenied(Exception):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


# A browser-shaped default User-Agent, for the same reason the host supplies a
# default Accept: every analysed source is written for a host whose network layer
# sends one, and an upstream that sees none may answer with something else
# entirely.  Measured 2026-09-17 against nmobi.kuwo.cn -- the same signed URL for
# `rid=128014` answers with that track when any User-Agent is present, and with an
# unrelated track (`rid=260839262`) when the header is absent, which 玉宁熙-Pro
# then reports as "解析数据失败" and refuses to resolve.
DEFAULT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _hostname(host: str) -> str:
    try:
        return host.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, AttributeError):
        raise ActionDenied("url_denied", "invalid hostname")


@dataclass(frozen=True)
class EgressTarget:
    """One validated request target, already normalized for the socket."""

    parsed: SplitResult
    host: str
    target: str
    port: int
    secure: bool

    @property
    def authority(self) -> str:
        """The authority to put on the wire, with a non-default port spelled out."""
        default = DEFAULT_EGRESS_PORT if self.secure else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"


def coerce_egress_policy(value: EgressPolicy | PluginManifest | Iterable[str]) -> EgressPolicy:
    """Accept a policy, a manifest, or a bare list of exact hosts.

    The bare list is the strict shorthand the pre-policy callers used: implicit
    HTTPS on port 443 and nothing else.
    """
    if isinstance(value, EgressPolicy):
        return value
    if isinstance(value, PluginManifest):
        return value.egress
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ActionDenied("host_denied", "an egress policy is required")
    try:
        hosts = tuple(sorted({_hostname(item) for item in value}))
    except ActionDenied:
        raise
    except (TypeError, AttributeError) as exc:
        raise ActionDenied("host_denied", "an egress policy is required") from exc
    return EgressPolicy(allowed_hosts=hosts)


def _parse_action_url(url: str, policy: EgressPolicy) -> EgressTarget:
    """Validate one action URL against a policy and return its target.

    Every refusal names the policy field that would have to change, so an
    operator reading a denial knows which opt-in the source is missing.
    """
    if any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in url):
        raise ActionDenied("url_denied", "URL contains whitespace or controls")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError, UnicodeError):
        raise ActionDenied("url_denied", "malformed URL")
    if (parsed.scheme not in ("http", "https") or not host or parsed.username is not None or
            parsed.password is not None or parsed.fragment):
        raise ActionDenied("url_denied", "only absolute http or https URLs are allowed")
    secure = parsed.scheme == "https"
    if not secure and not policy.allow_insecure_http:
        raise ActionDenied("scheme_denied", "plain HTTP is not allowlisted for this source")
    effective_port = port if port is not None else (DEFAULT_EGRESS_PORT if secure else 80)
    # The scheme's own default port comes with the scheme grant; an explicit
    # port is a destination of its own and has to be listed.
    if port is not None and effective_port not in policy.allowed_ports:
        raise ActionDenied("port_denied", "port is not allowlisted for this source")
    approved = _hostname(host)
    if literal_address(approved) is not None and not policy.allow_ip_hosts:
        raise ActionDenied("host_denied", "IP literals are not allowlisted for this source")
    if not policy.permits(approved):
        raise ActionDenied("host_denied", "host is not allowlisted")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    if not target.isascii() or any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in target):
        raise ActionDenied("url_denied", "request target contains non-ASCII, whitespace, or controls")
    return EgressTarget(parsed=parsed, host=approved, target=target, port=effective_port, secure=secure)


def _request_bytes(action: HttpAction, target: EgressTarget) -> bytes:
    """Serialize one action; the plugin supplies data, never framing."""
    lines = [f"{action.method} {target.target} HTTP/1.1", f"Host: {target.authority}", "Connection: close"]
    declared = {name.lower() for name in action.headers}
    if "accept" not in declared:
        lines.append("Accept: application/json")
    if "user-agent" not in declared:
        lines.append(f"User-Agent: {DEFAULT_USER_AGENT}")
    body = base64.b64decode(action.body) if action.body else b""
    if body:
        lines.append(f"Content-Length: {len(body)}")
    lines.extend(f"{name}: {item}" for name, item in action.headers.items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def _resolve_global_addresses(resolver: Callable, host: str, port: int) -> list[tuple[int, tuple]]:
    """Resolve an exact host and reject malformed or non-global answers."""
    try:
        resolved = list(resolver(host, port))
        candidates: dict[tuple[int, str, int], tuple[int, tuple]] = {}
        for item in resolved:
            family, sockaddr = (item[0], item[4]) if len(item) >= 5 else (item[0], item[1])
            if not isinstance(sockaddr, tuple) or len(sockaddr) < 2:
                raise ActionDenied("address_denied", "malformed resolved address")
            try:
                ip = ipaddress.ip_address(sockaddr[0])
            except (ValueError, TypeError, IndexError) as exc:
                raise ActionDenied("address_denied", "malformed resolved address") from exc
            if ((family == socket.AF_INET and ip.version != 4) or
                    (family == socket.AF_INET6 and ip.version != 6) or
                    family not in (socket.AF_INET, socket.AF_INET6)):
                raise ActionDenied("address_denied", "address family mismatch")
            if not ip.is_global:
                raise ActionDenied("address_denied", "resolved address is not global")
            numeric = (str(ip), port) if family == socket.AF_INET else (str(ip), port, 0, 0)
            candidates[(family, str(ip), port)] = (family, numeric)
        if not candidates:
            raise ActionDenied("dns_error", "host did not resolve")
        return [value for _, value in sorted(candidates.items(), key=lambda item: (item[0][0], item[0][1]))]
    except ActionDenied:
        raise
    except (OSError, ValueError, TypeError, IndexError):
        raise ActionDenied("dns_error", "DNS resolution failed")


class HttpsActionBroker:
    def __init__(self, *, resolver: Callable | None = None, connector: Callable | None = None,
                 ssl_context: ssl.SSLContext | object | None = None, timeout: float = 10.0):
        self.resolver = resolver or self._resolve
        self.connector = connector or self._connect
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.timeout = timeout

    @staticmethod
    def _resolve(host: str, port: int):
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)

    @staticmethod
    def _connect(address, timeout):
        return socket.create_connection(address, timeout=timeout)

    def fetch(self, action: HttpAction, policy: EgressPolicy | PluginManifest | Iterable[str], *,
              timeout: float | None = None) -> HttpObservation:
        request_timeout = self.timeout if timeout is None else min(self.timeout, max(0.001, timeout))
        deadline = time.monotonic() + request_timeout
        target = _parse_action_url(action.url, coerce_egress_policy(policy))

        try:
            # A synchronous resolver cannot be interrupted; callers still enforce
            # the outer deadline and this check bounds connect/read work.
            candidates = _resolve_global_addresses(self.resolver, target.host, target.port)
            if time.monotonic() >= deadline:
                raise ActionDenied("timeout", "action timed out")
        except ActionDenied:
            raise
        except (OSError, ValueError, TypeError, IndexError):
            raise ActionDenied("dns_error", "DNS resolution failed")

        raw = wrapped = response = None
        try:
            for _, address in candidates:
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ActionDenied("timeout", "action timed out")
                    raw = self.connector(address, remaining)
                    break
                except OSError:
                    raw = None
            if raw is None:
                raise ActionDenied("connect_error", "connection failed")
            if target.secure:
                try:
                    wrapped = self.ssl_context.wrap_socket(raw, server_hostname=target.host)
                except Exception as exc:
                    raise ActionDenied("tls_error", "TLS negotiation failed") from exc
            else:
                # Plain HTTP is an explicit per-source opt-in; it still reaches
                # only the resolved, policy-approved address of one exact host.
                wrapped = raw
            def apply_deadline_timeout() -> None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ActionDenied("timeout", "action timed out")
                try:
                    wrapped.settimeout(max(0.001, remaining))
                except OSError as exc:
                    raise ActionDenied("connect_error", "connection failed") from exc
            apply_deadline_timeout()
            wrapped.sendall(_request_bytes(action, target))
            apply_deadline_timeout()
            response = http.client.HTTPResponse(wrapped)
            response.begin()
            if 300 <= response.status < 400:
                raise ActionDenied("redirect_denied", "redirects are not followed")
            selected: dict[str, str] = {}
            for key, value in response.getheaders():
                name = key.lower()
                if name not in {"content-type", "content-length", "etag", "last-modified"}:
                    continue
                if name in selected:
                    raise ActionDenied("http_error", "duplicate selected response header")
                if any(ord(char) < 32 or ord(char) == 127 for char in value) or len(value.encode("utf-8")) > 1024:
                    raise ActionDenied("http_error", "unsafe response header")
                selected[name] = value
            length = selected.get("content-length")
            if length is not None:
                if not length or not length.isascii() or not length.isdecimal():
                    raise ActionDenied("http_error", "invalid Content-Length")
                value = int(length)
                if value > MAX_ACTION_BODY_BYTES:
                    raise ActionDenied("body_too_large", "response body exceeds 1 MiB")
            apply_deadline_timeout()
            body = response.read(MAX_ACTION_BODY_BYTES + 1)
            apply_deadline_timeout()
            if len(body) > MAX_ACTION_BODY_BYTES:
                raise ActionDenied("body_too_large", "response body exceeds 1 MiB")
            return HttpObservation(action_id=action.action_id, status_code=response.status,
                                   headers=selected, body=base64.b64encode(body).decode("ascii"))
        except ActionDenied:
            raise
        except (http.client.HTTPException, OSError, ValueError) as exc:
            raise ActionDenied("http_error", "invalid HTTP response") from exc
        finally:
            if response is not None:
                response.close()
            if wrapped is not None:
                wrapped.close()
            elif raw is not None:
                raw.close()
