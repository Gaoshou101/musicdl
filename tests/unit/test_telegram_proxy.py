"""A configured proxy has to reach Telethon in a shape it can actually use.

Telethon takes a tuple or a mapping, and needs ``python-socks`` to open the
tunnel. A URL string raises ``TypeError: Proxy of unknown format``, and a
missing dependency makes it drop the argument after a single warning -- so both
failure modes used to reach the operator as "Telegram is not connecting", with
the container log holding the only clue. Each one is pinned here.
"""

import asyncio
import sys
import types

import pytest

from musicdl.telegram import proxy as proxy_module
from musicdl.telegram.connector import TelegramConnector, telethon_client_factory
from musicdl.telegram.models import TelegramStatus
from musicdl.telegram.proxy import TelegramProxyError, parse_proxy, resolve_proxy


@pytest.mark.parametrize("value, expected", [
    ("socks5://127.0.0.1:1080", ("socks5", "127.0.0.1", 1080, True, None, None)),
    ("socks4://127.0.0.1:1080", ("socks4", "127.0.0.1", 1080, True, None, None)),
    ("http://192.168.10.30:7890", ("http", "192.168.10.30", 7890, True, None, None)),
    ("HTTPS://proxy.example.com:8443", ("http", "proxy.example.com", 8443, True, None, None)),
    ("socks5://user:p%40ss@10.0.0.1:1080", ("socks5", "10.0.0.1", 1080, True, "user", "p@ss")),
    ("  socks5://127.0.0.1:1080  ", ("socks5", "127.0.0.1", 1080, True, None, None)),
])
def test_parse_proxy_builds_the_tuple_telethon_accepts(value, expected):
    assert parse_proxy(value) == expected


@pytest.mark.parametrize("value", [None, "", "   "])
def test_parse_proxy_treats_an_absent_setting_as_no_proxy(value):
    assert parse_proxy(value) is None


@pytest.mark.parametrize("value", [
    "127.0.0.1:1080",             # the scheme is the easy thing to leave out, and Telethon cannot guess it
    "socks5://127.0.0.1",         # no port
    "socks5://127.0.0.1:notaport",
    "socks5://:1080",             # no host
    "ftp://127.0.0.1:1080",       # a scheme Telethon does not speak
    "socks5h://127.0.0.1:1080",
])
def test_parse_proxy_rejects_what_telethon_cannot_use(value):
    with pytest.raises(TelegramProxyError):
        parse_proxy(value)


def test_a_rejected_proxy_never_repeats_the_setting():
    with pytest.raises(TelegramProxyError) as rejected:
        parse_proxy("socks5://operator:sup3rsecret@127.0.0.1")  # the port is what is missing
    assert "sup3rsecret" not in str(rejected.value)


def test_resolve_proxy_needs_the_dependency_telethon_tunnels_with(monkeypatch):
    monkeypatch.setattr(proxy_module, "_dependency_present", lambda: False)
    with pytest.raises(TelegramProxyError) as rejected:
        resolve_proxy("socks5://127.0.0.1:1080")
    assert "python-socks" in str(rejected.value)


def test_resolve_proxy_asks_for_no_dependency_when_no_proxy_is_set(monkeypatch):
    monkeypatch.setattr(proxy_module, "_dependency_present", lambda: False)
    assert resolve_proxy(None) is None


def test_a_missing_dependency_is_detected_by_the_import_itself(monkeypatch):
    """The guard has to hold in a real image, not only against a patched helper."""
    monkeypatch.setitem(sys.modules, "python_socks", None)
    assert proxy_module._dependency_present() is False


def test_the_factory_hands_telethon_a_tuple_rather_than_the_url(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy_module, "_dependency_present", lambda: True)
    captured = {}

    class Client:
        def __init__(self, session, api_id, api_hash, **kwargs):
            captured.update(kwargs)

    fake_telethon = types.ModuleType("telethon")
    fake_telethon.TelegramClient = Client
    monkeypatch.setitem(sys.modules, "telethon", fake_telethon)
    telethon_client_factory(7, "hash", "socks5://127.0.0.1:1080")(tmp_path / "alice")
    assert captured["proxy"] == ("socks5", "127.0.0.1", 1080, True, None, None)


def test_the_operator_is_told_why_a_proxy_could_not_be_used(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy_module, "_dependency_present", lambda: False)
    factory = telethon_client_factory(7, "hash", "socks5://127.0.0.1:1080")
    connector = TelegramConnector(tmp_path, 7, "hash", factory)
    result = asyncio.run(connector.begin_login("alice", "+100"))
    assert result.status is TelegramStatus.ERROR
    assert "python-socks" in (result.error or "")
