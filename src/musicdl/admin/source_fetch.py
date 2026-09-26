"""Fetch one source file through the main process' constrained HTTP broker.

The admin import screen accepts URLs only as a convenience for an operator.  A
fetch is therefore deliberately smaller than a plugin HTTP action: one GET,
one exact host, one standard HTTP(S) port, and no state carried in from the
browser.  This module stays synchronous at the transport boundary; the portal
offloads it to a worker thread and puts an overall deadline around that call.
"""

from __future__ import annotations

import base64
import ipaddress
import re
import socket
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from musicdl.contracts.plugin import (
    EgressPolicy,
    HttpAction,
    MAX_ACTION_URL_BYTES,
    MAX_SOURCE_BYTES,
    literal_address,
)
from musicdl.plugins.broker import ActionDenied, HttpsActionBroker


# The broker owns the per-request budget.  The portal's async wrapper has a
# slightly larger outer budget so a slow synchronous resolver cannot hold the
# request forever after the broker's own 10 second deadline has elapsed.
SOURCE_FETCH_TIMEOUT = 10.0
SOURCE_FETCH_DEADLINE = 12.0
# The browser imports a batch sequentially.  A small per-router pool keeps
# independent admin requests moving while preventing repeated timed-out
# synchronous DNS calls from filling the default executor.
SOURCE_FETCH_CONCURRENCY = 4

_SOURCE_ACTION_ID = "admin-source-fetch"
_SOURCE_HTML = re.compile(r"^<(?:!doctype\s+html|html(?:\s|>)|head(?:\s|>)|body(?:\s|>))",
                          re.IGNORECASE)
_SAFE_FILENAME = re.compile(r"[^\w .()\[\]-]", re.UNICODE)


class SourceFetchError(Exception):
    """A stable, non-sensitive source import failure.

    The exception intentionally stores only the public error code.  In
    particular, no URL, query string, response body, or underlying exception
    text is retained for a route or an audit/event logger to accidentally
    expose.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _deny(code: str) -> SourceFetchError:
    return SourceFetchError(code)


def _normalize_hostname(host: str) -> str:
    try:
        normalized = host.rstrip(".").encode("idna").decode("ascii").lower()
    except (AttributeError, UnicodeError):
        raise _deny("url_denied") from None
    if not normalized:
        raise _deny("url_denied")
    return normalized


def _validate_url(url: str) -> tuple[Any, str, int, bool]:
    """Validate the import target before a resolver or connector can run."""
    if not isinstance(url, str) or not url:
        raise _deny("url_denied")
    try:
        if len(url.encode("utf-8", "strict")) > MAX_ACTION_URL_BYTES:
            raise _deny("url_denied")
    except UnicodeEncodeError:
        raise _deny("url_denied") from None
    if any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in url):
        raise _deny("url_denied")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        explicit_port = parsed.port
    except (TypeError, ValueError, UnicodeError):
        raise _deny("url_denied") from None
    if (parsed.scheme not in {"http", "https"} or not hostname or
            parsed.username is not None or parsed.password is not None or parsed.fragment):
        raise _deny("url_denied")
    host = _normalize_hostname(hostname)
    if literal_address(host) is not None:
        raise _deny("host_denied")
    secure = parsed.scheme == "https"
    standard_port = 443 if secure else 80
    if explicit_port is not None and explicit_port != standard_port:
        raise _deny("port_denied")
    return parsed, host, standard_port, secure


def _public_dns_address(family: int, sockaddr: Any) -> bool:
    """Whether one resolver answer is safe to hand to the broker.

    ``ipaddress.IPv4Address.is_global`` is not sufficient for multicast on
    every supported Python release, and IPv4-mapped IPv6 addresses need their
    embedded IPv4 address checked as well.  Keep all answers subject to the
    same rule; a single private/multicast answer rejects a mixed result rather
    than allowing the connector to try a later public address.
    """
    if family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(sockaddr, tuple) or len(sockaddr) < 2:
        raise _deny("address_denied")
    try:
        address = ipaddress.ip_address(sockaddr[0])
    except (IndexError, TypeError, ValueError):
        raise _deny("address_denied") from None
    if ((family == socket.AF_INET and address.version != 4) or
            (family == socket.AF_INET6 and address.version != 6)):
        raise _deny("address_denied")
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None and not mapped.is_global:
        raise _deny("address_denied")
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise _deny("address_denied")
    return True


def _guarded_resolver(resolver: Callable) -> Callable:
    """Wrap a resolver with source-import-specific global-address checks."""
    def resolve(host: str, port: int):
        try:
            answers = list(resolver(host, port))
        except ActionDenied:
            raise
        except Exception:  # noqa: BLE001 - fixed public code below
            raise ActionDenied("dns_error", "DNS resolution failed") from None
        if not answers:
            raise ActionDenied("dns_error", "host did not resolve")
        for answer in answers:
            try:
                family, sockaddr = (answer[0], answer[4]) if len(answer) >= 5 else (answer[0], answer[1])
            except (IndexError, TypeError):
                raise ActionDenied("address_denied", "malformed resolved address") from None
            try:
                _public_dns_address(family, sockaddr)
            except SourceFetchError as exc:
                raise ActionDenied(exc.code, exc.code) from None
        return answers
    return resolve


def _filename(parsed: Any) -> str:
    """Derive a harmless display filename from the URL path only."""
    path = parsed.path or ""
    try:
        decoded = unquote(path, errors="strict")
    except (UnicodeError, TypeError):
        decoded = path
    # Decode before selecting the component so an escaped slash cannot make a
    # path separator part of the returned filename.  Query and fragment are
    # intentionally never consulted.
    leaf = decoded.replace("\\", "/").rsplit("/", 1)[-1]
    leaf = _SAFE_FILENAME.sub("_", leaf).strip(" .")
    if not leaf or leaf in {".", ".."}:
        return "source.js"
    # A filename is display metadata, not a storage path.  Keep the result
    # bounded while retaining a useful extension for language inference.
    encoded = leaf.encode("utf-8")
    if len(encoded) > 255:
        leaf = encoded[:255].decode("utf-8", "ignore").rstrip(" .") or "source.js"
    return leaf


def _language(filename: str) -> str:
    return "python" if filename.casefold().endswith(".py") else "javascript"


def _source_from_observation(observation: Any, filename: str) -> dict[str, str]:
    """Validate and decode one broker observation without exposing its body."""
    status = getattr(observation, "status_code", None)
    if not isinstance(status, int) or not 200 <= status < 300:
        raise _deny("http_error")
    headers = getattr(observation, "headers", {})
    content_type = headers.get("content-type", "") if isinstance(headers, dict) else ""
    if isinstance(content_type, str) and content_type.split(";", 1)[0].strip().casefold() in {
            "text/html", "application/xhtml+xml"}:
        raise _deny("source_html")
    body = getattr(observation, "body", None)
    if not isinstance(body, str):
        raise _deny("http_error")
    try:
        raw = base64.b64decode(body, validate=True)
    except (TypeError, ValueError):
        raise _deny("http_error") from None
    # Check the source ceiling before decoding.  This is both the import
    # contract and protection against a malformed UTF-8 payload consuming more
    # work than the caller's source limit permits.
    if len(raw) > MAX_SOURCE_BYTES:
        raise _deny("source_too_large")
    if not raw:
        raise _deny("source_empty")
    if b"\x00" in raw:
        raise _deny("source_nul")
    try:
        script = raw.decode("utf-8-sig", "strict")
    except UnicodeDecodeError:
        raise _deny("source_encoding") from None
    if not script.strip():
        raise _deny("source_empty")
    if _SOURCE_HTML.match(script.lstrip(" \t\r\n")):
        raise _deny("source_html")
    return {"script": script, "filename": filename, "language": _language(filename)}


def _stable_action_error(error: ActionDenied) -> SourceFetchError:
    allowed = {
        "url_denied", "scheme_denied", "port_denied", "host_denied", "address_denied",
        "dns_error", "connect_error", "tls_error", "timeout", "redirect_denied",
        "body_too_large", "http_error",
    }
    return _deny(error.code if error.code in allowed else "fetch_failed")


def fetch_source(url: str, *, broker: HttpsActionBroker | Any | None = None,
                 resolver: Callable | None = None, timeout: float = SOURCE_FETCH_TIMEOUT) -> dict[str, str]:
    """Fetch and validate a source URL using the main-process broker.

    ``broker`` and ``resolver`` are explicit seams for unit tests.  Production
    callers leave them unset, which constructs a broker with the guarded DNS
    resolver and its fixed 10 second transport budget.
    """
    parsed, host, port, secure = _validate_url(url)
    filename = _filename(parsed)
    policy = EgressPolicy(allowed_hosts=(host,), allowed_ports=(port,),
                          allow_insecure_http=not secure)
    action = HttpAction(action_id=_SOURCE_ACTION_ID, method="GET", url=url, headers={}, body="")
    if broker is None:
        resolver = resolver or HttpsActionBroker._resolve
        broker = HttpsActionBroker(resolver=_guarded_resolver(resolver), timeout=SOURCE_FETCH_TIMEOUT)
    try:
        bounded_timeout = min(SOURCE_FETCH_TIMEOUT, max(0.001, float(timeout)))
    except (TypeError, ValueError, OverflowError):
        raise _deny("timeout") from None
    try:
        observation = broker.fetch(action, policy, timeout=bounded_timeout)
    except SourceFetchError:
        raise
    except ActionDenied as exc:
        raise _stable_action_error(exc) from None
    except Exception:  # noqa: BLE001 - network/connector errors are fixed-code failures
        raise _deny("fetch_failed") from None
    return _source_from_observation(observation, filename)


__all__ = [
    "SOURCE_FETCH_DEADLINE",
    "SOURCE_FETCH_CONCURRENCY",
    "SOURCE_FETCH_TIMEOUT",
    "SourceFetchError",
    "fetch_source",
]
