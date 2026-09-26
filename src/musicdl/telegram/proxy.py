"""The one place that turns the operator's proxy into what Telethon accepts.

Telethon opens a tunnel only for a ``(proxy_type, addr, port, rdns, username,
password)`` tuple or an equivalent mapping, and only when ``python-socks`` is
importable. Handed the URL string the panel collects it raises ``TypeError:
Proxy of unknown format``; without the dependency it drops the argument and
warns once -- a warning a container log has lost long before an operator thinks
to look, which makes a configured-but-ignored proxy indistinguishable from a
network that is down.

Both cases are therefore errors here, and the message never repeats the value:
the panel renders it, and a proxy URL may carry credentials.
"""

from urllib.parse import unquote, urlsplit

# Telethon's ``proxy_type`` vocabulary. An ``https`` proxy is the same CONNECT
# tunnel as ``http``, so both map onto the same entry.
_SCHEMES = {"socks5": "socks5", "socks4": "socks4", "http": "http", "https": "http"}


class TelegramProxyError(ValueError):
    """A proxy setting this deployment cannot honour."""


def _dependency_present() -> bool:
    """Whether this image carries the dependency Telethon tunnels with."""
    try:
        import python_socks  # noqa: F401
    except ImportError:
        return False
    return True


def parse_proxy(value: str | None):
    """The Telethon proxy tuple for ``value``, or ``None`` when nothing is set."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TelegramProxyError("proxy must be a URL string")
    text = value.strip()
    if not text:
        return None
    parts = urlsplit(text)
    proxy_type = _SCHEMES.get(parts.scheme)
    if proxy_type is None or not parts.hostname:
        raise TelegramProxyError(
            "proxy must be scheme://[user:pass@]host:port, where scheme is one of "
            "socks5, socks4, http or https")
    try:
        port = parts.port
    except ValueError:
        raise TelegramProxyError("proxy port must be a number") from None
    if port is None:
        raise TelegramProxyError("proxy must name a port")
    return (proxy_type, parts.hostname, port, True,
            unquote(parts.username) if parts.username else None,
            unquote(parts.password) if parts.password else None)


def resolve_proxy(value: str | None):
    """What to hand Telethon, with the dependency it needs checked first.

    Called where the client is built rather than where settings load, so that a
    proxy this deployment cannot honour costs the Telegram integration instead
    of the whole service: the settings model is rebuilt at start-up, and a value
    refused there would keep the panel from coming up at all.
    """
    proxy = parse_proxy(value)
    if proxy is None:
        return None
    if not _dependency_present():
        raise TelegramProxyError(
            "a proxy is configured but python-socks is missing from this image, so the "
            "proxy would be ignored and Telegram would be dialled directly; install the "
            "dependency or clear the setting")
    return proxy
