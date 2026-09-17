"""Main-process policy-checked streaming for resolved plugin media."""

from __future__ import annotations

import asyncio
import http.client
import socket
import ssl
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin

from musicdl.contracts.plugin import RESOLVED_MEDIA_TYPES, ResolvedMedia
from musicdl.plugins.broker import (
    ActionDenied, EgressPolicy, EgressTarget, PluginManifest, _parse_action_url,
    _resolve_global_addresses, coerce_egress_policy,
)

from .models import MAX_MEDIA_BYTES, DownloadMetadata, MediaError, _CloseOnce

MAX_RESPONSE_HEADER_COUNT = 64
MAX_RESPONSE_HEADER_FIELD_BYTES = 8 * 1024
MAX_RESPONSE_HEADERS_BYTES = 64 * 1024


class MediaTransportError(MediaError):
    """Stable, redacted failure from the main-owned media transport."""


def _remaining(clock: Callable[[], float], deadline: float) -> float:
    value = deadline - clock()
    if value <= 0:
        raise MediaTransportError("media_timeout")
    return value


def _target_for_url(url: str, policy: EgressPolicy) -> EgressTarget:
    """Resolve one URL against a policy, naming the offending layer.

    The URL shape is refused as ``media_url_denied`` and a destination the
    policy does not reach as ``media_host_denied``, so an operator can tell a
    malformed plugin answer from a missing grant.  Every hop of a redirect
    chain goes through here, so a redirect cannot reach a host, scheme, or port
    the source was never granted.
    """
    try:
        return _parse_action_url(url, policy)
    except ActionDenied as exc:
        raise MediaTransportError(
            "media_host_denied" if exc.code == "host_denied" else "media_url_denied") from exc


def _close_sync(value: object) -> None:
    close = getattr(value, "close", None)
    if close is not None:
        close()


@dataclass(frozen=True)
class _Hop:
    """One opened response: what it said, and the sockets still behind it."""

    target: EgressTarget
    status: int
    headers: dict[str, str]
    content_length: int | None
    content_type: str | None
    response: object
    wrapped: object
    raw: object


class SecureMediaTransport:
    def __init__(
        self,
        *,
        resolver: Callable[[str, int], Iterable[tuple]] | None = None,
        connector: Callable[[tuple, float], socket.socket] | None = None,
        tls_wrap: Callable[[socket.socket, str], socket.socket] | None = None,
        response_factory: Callable[[socket.socket], http.client.HTTPResponse] | None = None,
        clock: Callable[[], float] = time.monotonic,
        default_timeout_ms: int = 30_000,
        max_bytes: int = MAX_MEDIA_BYTES,
        chunk_size: int = 64 * 1024,
        max_redirects: int = 4,
    ):
        if isinstance(default_timeout_ms, bool) or not isinstance(default_timeout_ms, int) or not 0 < default_timeout_ms <= 30_000:
            raise ValueError("invalid_timeout")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 < max_bytes <= MAX_MEDIA_BYTES:
            raise ValueError("invalid_max_bytes")
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
            raise ValueError("invalid_chunk_size")
        if isinstance(max_redirects, bool) or not isinstance(max_redirects, int) or not 0 <= max_redirects <= 10:
            raise ValueError("invalid_max_redirects")
        self.resolver = resolver or self._resolve
        self.connector = connector or self._connect
        self._ssl_context = ssl.create_default_context()
        self.tls_wrap = tls_wrap or self._wrap_tls
        self.response_factory = response_factory or http.client.HTTPResponse
        self.clock = clock
        self.default_timeout_ms = default_timeout_ms
        self.max_bytes = max_bytes
        self.chunk_size = chunk_size
        self.max_redirects = max_redirects
        self._active: list[_CloseOnce] = []
        self._active_lock = asyncio.Lock()

    @staticmethod
    def _resolve(host: str, port: int):
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)

    @staticmethod
    def _connect(address, timeout):
        return socket.create_connection(address, timeout=timeout)

    def _wrap_tls(self, sock: socket.socket, server_hostname: str) -> socket.socket:
        return self._ssl_context.wrap_socket(sock, server_hostname=server_hostname)

    async def _run(self, operation: Callable[[], object], deadline: float) -> object:
        try:
            return await asyncio.wait_for(asyncio.to_thread(operation), _remaining(self.clock, deadline))
        except MediaTransportError:
            raise
        except asyncio.TimeoutError as exc:
            raise MediaTransportError("media_timeout") from exc

    async def _run_acquire(self, operation: Callable[[], object], deadline: float) -> object:
        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.wait_for(asyncio.shield(task), _remaining(self.clock, deadline))
        except asyncio.CancelledError as cancellation:
            result = await self._drain(task)
            if result is not None:
                await asyncio.to_thread(_close_sync, result)
            raise cancellation
        except asyncio.TimeoutError as exc:
            result = await self._drain(task)
            if result is not None:
                await asyncio.to_thread(_close_sync, result)
            raise MediaTransportError("media_timeout") from exc

    @staticmethod
    async def _drain(task: asyncio.Task) -> object | None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not task.done() or task.cancelled():
            return None
        try:
            return task.result()
        except BaseException:
            return None

    async def _register(self, closer: _CloseOnce) -> None:
        async with self._active_lock:
            self._active.append(closer)

    async def _unregister(self, closer: _CloseOnce) -> None:
        async with self._active_lock:
            if closer in self._active:
                self._active.remove(closer)

    async def _close_handles(self, handles: tuple[object, ...], closer: _CloseOnce | None = None) -> None:
        first_error: BaseException | None = None
        seen: set[int] = set()
        for handle in handles:
            if handle is None or id(handle) in seen:
                continue
            seen.add(id(handle))
            try:
                await asyncio.to_thread(_close_sync, handle)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if closer is not None:
            await self._unregister(closer)
        if first_error is not None:
            raise first_error

    @staticmethod
    def _response_headers(response: http.client.HTTPResponse) -> tuple[dict[str, str], int | None, str | None]:
        try:
            raw_headers = list(response.getheaders())
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise MediaTransportError("media_response_invalid") from exc
        if len(raw_headers) > MAX_RESPONSE_HEADER_COUNT:
            raise MediaTransportError("media_response_invalid")
        selected: dict[str, str] = {}
        aggregate = 0
        for key, value in raw_headers:
            if not isinstance(key, str) or not isinstance(value, str):
                raise MediaTransportError("media_response_invalid")
            field_size = len(key.encode("utf-8")) + len(value.encode("utf-8")) + 4
            aggregate += field_size
            if field_size > MAX_RESPONSE_HEADER_FIELD_BYTES or aggregate > MAX_RESPONSE_HEADERS_BYTES:
                raise MediaTransportError("media_response_invalid")
            if any(ord(char) < 32 or ord(char) == 127 for char in key + value):
                raise MediaTransportError("media_response_invalid")
            name = key.casefold()
            if name in selected:
                raise MediaTransportError("media_response_invalid")
            selected[name] = value.strip()

        encoding = selected.get("content-encoding")
        if encoding is not None and encoding.casefold() != "identity":
            raise MediaTransportError("media_response_invalid")
        length_header = selected.get("content-length")
        content_length: int | None = None
        if length_header is not None:
            if not length_header or not length_header.isascii() or not length_header.isdecimal():
                raise MediaTransportError("media_response_invalid")
            content_length = int(length_header)
        return selected, content_length, selected.get("content-type")

    async def _discard_handles(self, handles: tuple[object, ...]) -> None:
        """Close handles whose bytes will never be read.

        A redirect target is fetched on fresh sockets, and a close error on the
        abandoned hop must not replace the outcome the caller is waiting for.
        """
        try:
            await self._close_handles(handles)
        except BaseException:
            pass

    async def _fetch_hop(self, url: str, egress: EgressPolicy, deadline: float) -> _Hop:
        """Open one hop and read nothing past its status line and headers.

        The caller owns the returned handles on success; every failure path
        closes whatever this hop already acquired, so a redirect chain that
        ends in a refusal leaves no socket behind.
        """
        target = _target_for_url(url, egress)
        approved, port = target.host, target.port
        raw = wrapped = response = None
        try:
            try:
                candidates = await self._run(
                    lambda: _resolve_global_addresses(self.resolver, approved, port), deadline)
            except ActionDenied as exc:
                code = "media_address_denied" if exc.code == "address_denied" else "media_dns_failed"
                raise MediaTransportError(code) from exc
            except MediaError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise MediaTransportError("media_dns_failed") from exc
            for _, address in candidates:
                try:
                    raw = await self._run_acquire(lambda address=address: self.connector(address, _remaining(self.clock, deadline)), deadline)
                    break
                except MediaError as exc:
                    if exc.code == "media_timeout":
                        raise
                    raw = None
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raw = None
            if raw is None:
                raise MediaTransportError("media_connect_failed")
            if target.secure:
                try:
                    wrapped = await self._run_acquire(lambda: self.tls_wrap(raw, approved), deadline)
                except MediaError:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise MediaTransportError("media_tls_failed") from exc
            else:
                # Plain HTTP is a per-source operator grant. The socket still
                # goes to the pinned address of one policy-approved host.
                wrapped = raw

            def send_request() -> None:
                remaining = _remaining(self.clock, deadline)
                setter = getattr(wrapped, "settimeout", None)
                if setter is not None:
                    setter(max(0.001, remaining))
                request = (f"GET {target.target} HTTP/1.1\r\nHost: {target.authority}\r\n"
                           "Accept: application/octet-stream\r\n"
                           "Accept-Encoding: identity\r\n"
                           "Connection: close\r\n\r\n").encode("ascii")
                wrapped.sendall(request)

            await self._run(send_request, deadline)
            try:
                response = await self._run_acquire(lambda: self.response_factory(wrapped), deadline)
            except MediaError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise MediaTransportError("media_response_invalid") from exc

            def begin_response() -> None:
                remaining = _remaining(self.clock, deadline)
                setter = getattr(wrapped, "settimeout", None)
                if setter is not None:
                    setter(max(0.001, remaining))
                response.begin()

            await self._run(begin_response, deadline)
            headers, content_length, content_type = await self._run(
                lambda: self._response_headers(response), deadline)
            return _Hop(target=target, status=int(getattr(response, "status", 0)), headers=headers,
                        content_length=content_length, content_type=content_type,
                        response=response, wrapped=wrapped, raw=raw)
        except BaseException:
            await self._discard_handles((response, wrapped, raw))
            raise

    async def open(
        self,
        media: ResolvedMedia,
        *,
        policy: EgressPolicy | PluginManifest | Iterable[str],
        timeout_ms: int | None = None,
    ) -> DownloadMetadata:
        if timeout_ms is None:
            timeout_ms = self.default_timeout_ms
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
            raise MediaTransportError("media_timeout")
        deadline = self.clock() + timeout_ms / 1000
        egress = coerce_egress_policy(policy)

        raw = wrapped = response = None
        close_once: _CloseOnce | None = None
        try:
            # A CDN link that answers 3xx is followed, but only to a target this
            # same policy already reaches: every hop repeats the exact-host
            # allowlist check, the scheme grant, and the global-address check on
            # the addresses actually resolved for that hop.  A loop, a missing
            # Location, or a chain longer than ``max_redirects`` stays a
            # ``media_redirect_denied`` refusal.
            url = media.url
            for redirects in range(self.max_redirects + 1):
                hop = await self._fetch_hop(url, egress, deadline)
                response, wrapped, raw = hop.response, hop.wrapped, hop.raw
                if not 300 <= hop.status < 400:
                    break
                location = hop.headers.get("location")
                if not location or redirects == self.max_redirects:
                    raise MediaTransportError("media_redirect_denied")
                url = urljoin(url, location)
                await self._discard_handles((response, wrapped, raw))
                raw = wrapped = response = None
            if not 200 <= hop.status < 300:
                raise MediaTransportError("media_response_invalid")
            expected_types = RESOLVED_MEDIA_TYPES[media.extension]
            if hop.content_type is None or hop.content_type.split(";", 1)[0].strip().casefold() not in expected_types:
                raise MediaTransportError("media_response_invalid")
            content_length = hop.content_length
            if content_length is not None and content_length > self.max_bytes:
                raise MediaError("file_too_large")
            if media.declared_size is not None and content_length is not None and content_length != media.declared_size:
                raise MediaError("size_mismatch")

            async def close_response() -> None:
                await self._close_handles((response, wrapped, raw), close_once)

            close_once = _CloseOnce(close_response)
            await self._register(close_once)
            observed = 0

            async def chunks():
                nonlocal observed
                primary: BaseException | None = None
                try:
                    while True:
                        def read_chunk() -> bytes:
                            remaining = _remaining(self.clock, deadline)
                            setter = getattr(wrapped, "settimeout", None)
                            if setter is not None:
                                setter(max(0.001, remaining))
                            value = response.read(self.chunk_size)
                            if not isinstance(value, bytes):
                                raise MediaTransportError("media_response_invalid")
                            return value

                        try:
                            chunk = await self._run(read_chunk, deadline)
                        except MediaTransportError:
                            raise
                        except (OSError, http.client.HTTPException, ValueError) as exc:
                            raise MediaTransportError("media_response_invalid") from exc
                        if not chunk:
                            break
                        observed += len(chunk)
                        if observed > self.max_bytes:
                            raise MediaError("file_too_large")
                        if media.declared_size is not None and observed > media.declared_size:
                            raise MediaError("size_mismatch")
                        yield chunk
                    if content_length is not None and observed != content_length:
                        raise MediaError("size_mismatch")
                    if media.declared_size is not None and observed != media.declared_size:
                        raise MediaError("size_mismatch")
                except BaseException as exc:
                    primary = exc
                    raise
                finally:
                    try:
                        await close_once.close()
                    except BaseException:
                        if primary is None:
                            raise

            return DownloadMetadata(chunks=chunks(), extension=media.extension,
                                    media_type=media.media_type, declared_size=media.declared_size,
                                    _close_once=close_once)
        except asyncio.CancelledError:
            if close_once is not None:
                try:
                    await close_once.close()
                except BaseException:
                    pass
            else:
                try:
                    await self._close_handles((response, wrapped, raw))
                except BaseException:
                    pass
            raise
        except BaseException:
            if close_once is not None:
                try:
                    await close_once.close()
                except BaseException:
                    pass
            else:
                try:
                    await self._close_handles((response, wrapped, raw))
                except BaseException:
                    pass
            raise

    async def aclose(self) -> None:
        async with self._active_lock:
            active = tuple(self._active)
        for closer in active:
            try:
                await closer.close()
            except BaseException:
                pass
