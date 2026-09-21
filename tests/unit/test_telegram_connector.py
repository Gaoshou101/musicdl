import pytest
import asyncio
import os
import sys
import types

from musicdl.telegram.connector import TelegramConnector
from musicdl.telegram.connector import telethon_client_factory
from musicdl.telegram.models import TelegramStatus


class SessionPasswordNeededError(Exception):
    pass


class PhoneCodeInvalidError(Exception):
    pass


class FloodWaitError(Exception):
    def __init__(self, seconds):
        self.seconds = seconds


class UnauthorizedError(Exception):
    pass


class FakeClient:
    def __init__(self, state="new"):
        self.state = state
        self.code = None
        self.password = None

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return self.state == "ready"

    async def send_code_request(self, phone):
        self.code = "1234"

    async def sign_in(self, phone=None, code=None, password=None):
        if password is not None:
            if password == "pw":
                self.state = "ready"
            return
        if code == "1234":
            raise SessionPasswordNeededError()
        raise PhoneCodeInvalidError()


def test_first_login_code_then_two_factor_ready(tmp_path):
    clients = {}
    def factory(path):
        return clients.setdefault(str(path), FakeClient())
    connector = TelegramConnector(tmp_path, 1, "hash", factory)
    assert asyncio.run(connector.begin_login("alice", "+100")).status is TelegramStatus.CODE_REQUIRED
    assert asyncio.run(connector.complete_code("alice", "+100", "1234")).status is TelegramStatus.PASSWORD_REQUIRED
    assert asyncio.run(connector.complete_password("alice", "pw")).status is TelegramStatus.READY


def test_first_login_code_can_complete_without_two_factor(tmp_path):
    class DirectClient(FakeClient):
        async def sign_in(self, phone=None, code=None, password=None):
            if code == "1234":
                self.state = "ready"
            else:
                raise PhoneCodeInvalidError()
    connector = TelegramConnector(tmp_path, 1, "hash", lambda path: DirectClient())
    asyncio.run(connector.begin_login("alice", "+100"))
    assert asyncio.run(connector.complete_code("alice", "+100", "1234")).status is TelegramStatus.READY


def test_code_sign_in_without_authorization_is_invalid_session(tmp_path):
    class IncompleteClient(FakeClient):
        async def sign_in(self, phone=None, code=None, password=None):
            return None

    connector = TelegramConnector(tmp_path, 1, "hash", lambda path: IncompleteClient())
    result = asyncio.run(connector.complete_code("alice", "+100", "1234"))
    assert result.status is TelegramStatus.INVALID_SESSION


def test_password_sign_in_without_authorization_is_invalid_session(tmp_path):
    class IncompleteClient(FakeClient):
        async def sign_in(self, phone=None, code=None, password=None):
            return None

    connector = TelegramConnector(tmp_path, 1, "hash", lambda path: IncompleteClient())
    result = asyncio.run(connector.complete_password("alice", "pw"))
    assert result.status is TelegramStatus.INVALID_SESSION


class FakeConversation:
    def __init__(self, events, response):
        self.events = events
        self.response = response

    async def __aenter__(self):
        self.events.append("enter")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.events.append("exit")

    async def send_message(self, command):
        self.events.append(("send", command))

    async def get_response(self):
        self.events.append("response")
        return self.response


def test_bot_requester_sends_command_and_decodes_response(tmp_path):
    events = []

    class BotClient(FakeClient):
        def conversation(self, bot_username, timeout):
            events.append(("conversation", bot_username, timeout))
            return FakeConversation(events, {"media": "song"})

    connector = TelegramConnector(tmp_path, 1, "hash", lambda path: BotClient("ready"))
    requester = connector.bot_requester("alice", lambda response: [response["media"]])
    result = asyncio.run(requester("@bot", "/search song", 12.5))
    assert result == ["song"]
    assert events == [("conversation", "@bot", 12.5), "enter", ("send", "/search song"), "response", "exit"]


def test_unauthorized_bot_requester_does_not_open_conversation(tmp_path):
    opened = []

    class BotClient(FakeClient):
        def conversation(self, bot_username, timeout):
            opened.append((bot_username, timeout))
            return FakeConversation([], None)

    connector = TelegramConnector(tmp_path, 1, "hash", lambda path: BotClient("new"))
    requester = connector.bot_requester("alice", lambda response: response)
    with pytest.raises(RuntimeError, match="not authorized"):
        asyncio.run(requester("@bot", "/search", 10))
    assert opened == []


def test_bot_requester_rejects_invalid_decoder(tmp_path):
    connector = TelegramConnector(tmp_path, 1, "hash", lambda path: FakeClient("ready"))
    with pytest.raises(TypeError):
        connector.bot_requester("alice", None)


def test_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        TelegramConnector(tmp_path, 1, "hash", lambda path: FakeClient()).session_path("../escape")


def test_restart_restore_and_unauthorized(tmp_path):
    clients = {}
    def factory(path):
        return clients.setdefault(str(path), FakeClient("ready"))
    connector = TelegramConnector(tmp_path, 1, "hash", factory)
    assert asyncio.run(connector.restore("alice")).status is TelegramStatus.READY
    clients[str(connector.session_path("bad"))] = FakeClient("new")
    result = asyncio.run(connector.restore("bad"))
    assert result.status is TelegramStatus.INVALID_SESSION


def test_flood_wait_is_observable(tmp_path):
    class FloodClient(FakeClient):
        async def send_code_request(self, phone):
            raise FloodWaitError(17)
    connector = TelegramConnector(tmp_path, 1, "hash", lambda path: FloodClient())
    result = asyncio.run(connector.begin_login("alice", "+100"))
    assert result.status is TelegramStatus.RATE_LIMITED
    assert result.retry_after == 17


def test_status_does_not_expose_secrets():
    connector = TelegramConnector
    result = connector._failure(Exception("phone code password api_hash session"))
    rendered = repr(result) + str(result.to_dict())
    assert "phone" not in rendered and "password" not in rendered and "api_hash" not in rendered


def test_telethon_factory_is_lazy_and_configured(monkeypatch, tmp_path):
    captured = {}
    class Client:
        def __init__(self, session, api_id, api_hash, **kwargs):
            captured.update(session=session, api_id=api_id, api_hash=api_hash, **kwargs)
    fake_telethon = types.ModuleType("telethon")
    fake_telethon.TelegramClient = Client
    monkeypatch.setitem(sys.modules, "telethon", fake_telethon)
    client = telethon_client_factory(7, "hash")(tmp_path / "alice")
    assert isinstance(client, Client)
    assert captured == {"session": tmp_path / "alice", "api_id": 7, "api_hash": "hash", "flood_sleep_threshold": 0}


def test_persistence_uses_same_session_path_for_restart(tmp_path):
    state = {}
    class Persistent(FakeClient):
        def __init__(self, path):
            self.path = str(path)
            self.state = state.get(self.path, "new")
        async def sign_in(self, phone=None, code=None, password=None):
            await super().sign_in(phone, code, password)
            state[self.path] = self.state
    factory = lambda path: Persistent(path)
    first = TelegramConnector(tmp_path, 1, "hash", factory)
    asyncio.run(first.complete_code("alice", "+100", "bad"))
    asyncio.run(first.complete_password("alice", "pw"))
    second = TelegramConnector(tmp_path, 1, "hash", factory)
    assert asyncio.run(second.restore("alice")).status is TelegramStatus.READY


def test_session_artifacts_are_hardened(tmp_path):
    path = tmp_path / "alice"
    path.with_suffix(".session").write_text("credential")
    path.with_name("alice.session-wal").write_text("credential")
    path.with_name("alice.session-shm").write_text("credential")
    connector = TelegramConnector(tmp_path, 1, "hash", lambda p: FakeClient("ready"))
    asyncio.run(connector.restore("alice"))
    if os.name == "posix":
        assert all((path.with_name(name)).stat().st_mode & 0o077 == 0 for name in ("alice.session", "alice.session-wal", "alice.session-shm"))

def test_dotted_profile_session_is_hardened(tmp_path):
    path = tmp_path / "alice.work"
    session_file = path.with_name("alice.work.session")
    session_file.write_text("credential")
    connector = TelegramConnector(tmp_path, 1, "hash", lambda p: FakeClient("ready"))
    asyncio.run(connector.restore("alice.work"))
    assert session_file.exists()


def test_bot_requester_serializes_concurrent_requests(tmp_path):
    class ConcurrentClient(FakeClient):
        def __init__(self):
            super().__init__("ready")
            self.active = 0
            self.max_active = 0

        def conversation(self, bot, timeout):
            outer = self
            class Conv:
                async def __aenter__(self):
                    outer.active += 1
                    if outer.active > outer.max_active:
                        outer.max_active = outer.active
                    await asyncio.sleep(0.01)
                    return self
                async def __aexit__(self, *args):
                    outer.active -= 1
                async def send_message(self, cmd): pass
                async def get_response(self): return "ok"
            return Conv()

    client = ConcurrentClient()
    connector = TelegramConnector(tmp_path, 1, "hash", lambda p: client)
    requester = connector.bot_requester("default", lambda r: r)

    async def main():
        await asyncio.gather(
            requester("@bot", "cmd1", 10.0),
            requester("@bot", "cmd2", 10.0),
        )
    asyncio.run(main())
    assert client.max_active == 1


def test_connector_disconnect_clears_clients(tmp_path):
    class DisconnectClient(FakeClient):
        def __init__(self):
            super().__init__("ready")
            self.disconnected = False
        async def disconnect(self):
            self.disconnected = True

    client = DisconnectClient()
    connector = TelegramConnector(tmp_path, 1, "hash", lambda p: client)
    asyncio.run(connector.restore("default"))
    assert len(connector._clients) == 1
    asyncio.run(connector.disconnect())
    assert client.disconnected is True
    assert len(connector._clients) == 0
class FlowButton:
    def __init__(self, text):
        self.text = text


class FlowRow:
    def __init__(self, *labels):
        self.buttons = tuple(FlowButton(label) for label in labels)


class FlowMarkup:
    def __init__(self, *rows):
        self.rows = tuple(FlowRow(*row) for row in rows)


class FlowMessage:
    def __init__(self, *, text=None, markup=None, document=None):
        self.message, self.reply_markup, self.document = text, markup, document
        self.clicked = None

    async def click(self, index):
        self.clicked = index
        return self


class ScriptedConversation:
    """Hands back the replies a Bot would send, in order."""

    def __init__(self, replies, events):
        self.replies, self.events = list(replies), events

    async def __aenter__(self):
        self.events.append("enter")
        return self

    async def __aexit__(self, *exc):
        self.events.append("exit")
        return False

    async def send_message(self, command):
        self.events.append(("send", command))

    async def get_response(self, timeout=None):
        self.events.append(("response", timeout))
        return self.replies.pop(0)


class FlowClient(FakeClient):
    def __init__(self, replies, chunks=(), events=None, state="ready"):
        super().__init__(state)
        self.replies, self.chunks, self.events = replies, tuple(chunks), events if events is not None else []

    def conversation(self, bot_username, timeout):
        self.events.append(("conversation", bot_username, timeout))
        return ScriptedConversation(self.replies, self.events)

    async def iter_download(self, document, request_size=None):
        self.events.append(("iter_download", document, request_size))
        for chunk in self.chunks:
            yield chunk


def test_the_flow_presses_the_button_and_returns_the_file_behind_it(tmp_path):
    listing = FlowMessage(text="1. 晴天", markup=FlowMarkup(["1", "2"]))
    notice = FlowMessage(text="正在上传...")
    file = FlowMessage(document="the-file")
    client = FlowClient([listing, notice, file])
    connector = TelegramConnector(tmp_path, 1, "hash", lambda p: client)
    flow = connector.bot_flow("default")

    reply = asyncio.run(flow.select("music_v1bot", "/search 晴天", 10.0, "1"))

    assert reply is file
    assert listing.clicked == 0
    assert client.events[0] == ("conversation", "music_v1bot", 10.0)
    assert ("send", "/search 晴天") in client.events


def test_the_flow_reads_the_second_entry_as_the_second_button(tmp_path):
    listing = FlowMessage(text="1. a\n2. b", markup=FlowMarkup(["1", "2", "3"]))
    file = FlowMessage(document="the-file")
    client = FlowClient([listing, file])
    connector = TelegramConnector(tmp_path, 1, "hash", lambda p: client)

    asyncio.run(connector.bot_flow("default").select("bot", "/s", 5.0, "2"))

    assert listing.clicked == 1


def test_the_flow_refuses_a_button_that_is_not_there(tmp_path):
    listing = FlowMessage(text="1. a", markup=FlowMarkup(["上一页", "下一页"]))
    client = FlowClient([listing])
    connector = TelegramConnector(tmp_path, 1, "hash", lambda p: client)

    with pytest.raises(ValueError, match="bot_selection_missing"):
        asyncio.run(connector.bot_flow("default").select("bot", "/s", 5.0, "1"))


def test_the_flow_without_a_label_takes_the_file_the_bot_sends(tmp_path):
    file = FlowMessage(document="the-file")
    client = FlowClient([file])
    connector = TelegramConnector(tmp_path, 1, "hash", lambda p: client)

    reply = asyncio.run(connector.bot_flow("default").select("bot", "/s", 5.0))

    assert reply is file


def test_the_flow_gives_up_when_no_reply_carries_a_file(tmp_path):
    client = FlowClient([FlowMessage(text="没有找到相关歌曲")] * 8)
    connector = TelegramConnector(tmp_path, 1, "hash", lambda p: client)

    with pytest.raises(ValueError, match="invalid_bot_response"):
        asyncio.run(connector.bot_flow("default").select("bot", "/s", 5.0))


def test_the_flow_streams_the_file_the_reply_carries(tmp_path):
    file = FlowMessage(document="the-file")
    client = FlowClient([file], chunks=[b"fLaC", bytearray(b"\x00\x01"), b""])
    connector = TelegramConnector(tmp_path, 1, "hash", lambda p: client)
    flow = connector.bot_flow("default")

    async def drain():
        return [chunk async for chunk in flow.stream(file, request_size=4096)]

    assert asyncio.run(drain()) == [b"fLaC", b"\x00\x01"]
    assert client.events[-1] == ("iter_download", "the-file", 4096)


def test_the_flow_refuses_a_reply_without_a_file(tmp_path):
    client = FlowClient([])
    connector = TelegramConnector(tmp_path, 1, "hash", lambda p: client)
    flow = connector.bot_flow("default")

    async def drain():
        return [chunk async for chunk in flow.stream(FlowMessage(text="no file"))]

    with pytest.raises(ValueError, match="invalid_bot_response"):
        asyncio.run(drain())


def test_the_flow_reports_an_unauthorized_account_rather_than_asking(tmp_path):
    class Unauthorized(FlowClient):
        def conversation(self, bot_username, timeout):
            raise AssertionError("a conversation was opened for a signed-out account")

    client = Unauthorized([], state="new")
    connector = TelegramConnector(tmp_path, 1, "hash", lambda p: client)
    flow = connector.bot_flow("default")

    assert asyncio.run(flow.ready()) is False
    with pytest.raises(RuntimeError, match="not authorized"):
        asyncio.run(flow.ask("bot", "/search x", 5.0))


def test_the_flow_and_the_requester_share_one_lock_per_bot(tmp_path):
    connector = TelegramConnector(tmp_path, 1, "hash", lambda p: FakeClient("ready"))

    assert connector.bot_lock("default", "MusicBot") is connector.bot_lock("default", "musicbot")
    assert connector.bot_lock("default", "MusicBot") is not connector.bot_lock("other", "MusicBot")
