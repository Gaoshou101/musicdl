"""Main-owned, DNS-pinned HTTPS transport for plugin HTTP actions."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit
from typing import Callable, Iterable

from musicdl.contracts.plugin import MAX_ACTION_BODY_BYTES, HttpAction, HttpObservation


class ActionDenied(Exception):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


def _hostname(host: str) -> str:
    try:
        return host.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, AttributeError):
        raise ActionDenied("url_denied", "invalid hostname")


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

    def fetch(self, action: HttpAction, allowed_hosts: Iterable[str], *, timeout: float | None = None) -> HttpObservation:
        request_timeout = self.timeout if timeout is None else min(self.timeout, max(0.001, timeout))
        deadline = time.monotonic() + request_timeout
        if any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in action.url):
            raise ActionDenied("url_denied", "URL contains whitespace or controls")
        try:
            parsed = urlsplit(action.url)
            host = parsed.hostname
            port = parsed.port
        except (ValueError, UnicodeError):
            raise ActionDenied("url_denied", "malformed URL")
        if (parsed.scheme != "https" or not host or parsed.username is not None or
                parsed.password is not None or parsed.fragment or port is not None):
            raise ActionDenied("url_denied", "only implicit-port HTTPS URLs are allowed")
        approved = _hostname(host)
        allowed = {_hostname(item) for item in allowed_hosts}
        if approved not in allowed:
            raise ActionDenied("host_denied", "host is not allowlisted")
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        if not target.isascii() or any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in target):
            raise ActionDenied("url_denied", "request target contains non-ASCII, whitespace, or controls")

        try:
            # A synchronous resolver cannot be interrupted; callers still enforce
            # the outer deadline and this check bounds connect/read work.
            resolved = list(self.resolver(approved, 443))
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
                numeric = (str(ip), 443) if family == socket.AF_INET else (str(ip), 443, 0, 0)
                candidates[(family, str(ip), 443)] = (family, numeric)
            if not candidates:
                raise ActionDenied("dns_error", "host did not resolve")
            if time.monotonic() >= deadline:
                raise ActionDenied("timeout", "action timed out")
        except ActionDenied:
            raise
        except (OSError, ValueError, TypeError, IndexError):
            raise ActionDenied("dns_error", "DNS resolution failed")

        raw = wrapped = response = None
        try:
            for _, (family, address) in sorted(candidates.items(), key=lambda x: (x[0][0], x[0][1])):
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
            try:
                wrapped = self.ssl_context.wrap_socket(raw, server_hostname=approved)
            except Exception as exc:
                raise ActionDenied("tls_error", "TLS negotiation failed") from exc
            def apply_deadline_timeout() -> None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ActionDenied("timeout", "action timed out")
                try:
                    wrapped.settimeout(max(0.001, remaining))
                except OSError as exc:
                    raise ActionDenied("connect_error", "connection failed") from exc
            apply_deadline_timeout()
            wrapped.sendall((f"GET {target} HTTP/1.1\r\nHost: {approved}\r\n"
                             "Accept: application/json\r\nConnection: close\r\n\r\n").encode("ascii"))
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
            import base64
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
