"""The panel's own configuration layer: what it writes, and what it refuses."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from pydantic import SecretStr

from musicdl.admin import AdminStateStore, ConfigManager
from musicdl.app import create_app
from musicdl.config import AppSettings, WeComSettings

DEFAULT_LOGIN = {"username": "admin", "password": "password"}
OPERATOR_LOGIN = {"username": "operator", "password": "correct-horse-battery"}
AES_KEY = base64.b64encode(b"k" * 32).decode().rstrip("=")


def settings_for(tmp_path, name: str = "admin-state.json") -> AppSettings:
    """Settings that persist, with the POSIX-only path validator bypassed.

    The validator exists for the container deployment; a Windows checkout can
    only exercise the store against a real local path.
    """
    settings = AppSettings()
    settings.admin.state_path = str(tmp_path / name)
    return settings


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


def prepared_app(settings):
    """An application whose default password has already been replaced.

    Every route but the credential change answers ``403`` while the default
    credentials are in force, which is the deployment's own rule and not
    something these tests are about.
    """
    app = create_app(settings)
    app.state.admin.auth.change_credentials("admin", OPERATOR_LOGIN["username"],
                                            OPERATOR_LOGIN["password"])
    return app


def run(coro):
    return asyncio.run(coro)


def fields_of(payload: dict) -> dict[str, dict]:
    return {item["key"]: item for group in payload["groups"] for item in group["fields"]}


def test_describe_never_returns_a_secret_value(tmp_path):
    settings = AppSettings(wecom=WeComSettings(token=SecretStr("token-value"),
                                               secret=SecretStr("secret-value"),
                                               encoding_aes_key=SecretStr(AES_KEY)))
    payload = ConfigManager(settings).describe()
    rendered = json.dumps(payload)
    assert "token-value" not in rendered
    assert "secret-value" not in rendered
    assert AES_KEY not in rendered
    fields = fields_of(payload)
    assert fields["wecom.token"]["secret"] is True
    assert fields["wecom.token"]["set"] is True
    assert fields["wecom.token"]["value"] is None
    assert fields["wecom.enabled"]["value"] is False
    assert fields["wecom.allowed_users"]["value"] == []
    assert [group["id"] for group in payload["groups"]] == ["wecom", "redis", "telegram", "ai",
                                                            "worker", "admin"]


def test_every_field_names_the_environment_variable_that_sets_it(tmp_path):
    fields = fields_of(ConfigManager(settings_for(tmp_path)).describe())
    assert fields["worker.search_timeout"]["env"] == "MUSICDL_WORKER__SEARCH_TIMEOUT"
    assert fields["admin.login_limit"]["env"] == "MUSICDL_ADMIN__LOGIN_LIMIT"
    assert fields["admin.login_limit"]["scope"] == "hot"
    assert fields["worker.search_timeout"]["scope"] == "hot"


def test_no_editable_field_waits_for_a_restart(tmp_path):
    """Every field the panel offers is adopted by the running process.

    A field the panel can write but the process cannot adopt is a restart
    dressed up as a setting; the scopes exist so that stays visible, and the
    only two answers the panel may show are "now" and "next start".
    """
    fields = fields_of(ConfigManager(settings_for(tmp_path)).describe())
    assert {field["scope"] for field in fields.values()} == {"hot"}
    # The runtime groups are the ones a write has to rebuild for; the login
    # limiter belongs to the process, which retunes it in place.
    assert ConfigManager.affects_runtime("worker.search_timeout") is True
    assert ConfigManager.affects_runtime("wecom.enabled") is True
    assert ConfigManager.affects_runtime("admin.login_limit") is False
    assert ConfigManager.affects_runtime("not.a.setting") is False


def test_a_value_reports_the_layer_it_came_from(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICDL_AI__MODEL", "env-model")
    manager = ConfigManager(settings_for(tmp_path))
    assert fields_of(manager.describe())["ai.model"]["source"] == "env"
    manager.update({"ai.model": "panel-model"})
    field = fields_of(manager.describe())["ai.model"]
    assert (field["source"], field["value"]) == ("panel", "panel-model")
    manager.update({"ai.model": None})
    assert fields_of(manager.describe())["ai.model"]["source"] == "env"


def test_update_applies_live_and_persists(tmp_path):
    settings = settings_for(tmp_path)
    written: dict = {}
    manager = ConfigManager(settings, on_change=lambda: written.update(manager.snapshot()))
    manager.update({"ai.enabled": True, "ai.model": "m", "ai.api_key": "k"})
    assert settings.ai.enabled is True
    assert settings.ai.model == "m"
    assert settings.ai.api_key.get_secret_value() == "k"
    assert written == {"ai.enabled": True, "ai.model": "m", "ai.api_key": "k"}


def test_the_panel_owns_the_advisory_user_agent_and_a_blank_clears_it(tmp_path):
    """The gate is the request's agent, so the operator has to be able to set it."""
    settings = settings_for(tmp_path)
    manager = ConfigManager(settings)
    assert fields_of(manager.describe())["ai.user_agent"]["value"] is None
    manager.update({"ai.user_agent": "claude-cli/1.0.0 (external, cli)"})
    assert settings.ai.user_agent == "claude-cli/1.0.0 (external, cli)"
    assert fields_of(manager.describe())["ai.user_agent"]["source"] == "panel"
    manager.update({"ai.user_agent": ""})
    assert settings.ai.user_agent is None
    assert fields_of(manager.describe())["ai.user_agent"]["value"] is None


def test_the_container_knobs_are_reported_and_not_editable(tmp_path):
    manager = ConfigManager(settings_for(tmp_path))
    payload = manager.describe()
    knobs = {item["key"]: item for item in payload["container"]}
    # The console ships inside the app image and has no port of its own, so the
    # knob that used to advertise one now names where its files live.
    assert knobs["paths.panel"]["env"] == "MUSICDL_ADMIN__PANEL_ROOT"
    assert "ports.panel" not in knobs and "limits.panel" not in knobs
    assert "MUSICDL_PORT" == knobs["ports.app"]["env"]
    with pytest.raises(ValueError):
        manager.update({"media.root": "/somewhere-else"})


def test_nothing_is_stored_when_a_write_is_refused(tmp_path):
    manager = ConfigManager(settings_for(tmp_path))
    with pytest.raises(ValueError, match="unknown setting: ai.nope"):
        manager.update({"ai.nope": 1})
    with pytest.raises(ValueError):
        manager.update({"worker.max_attempts": 0})
    # A batch that is fine field by field but breaks the worker's own contract
    # has to be refused as a whole rather than half applied.
    with pytest.raises(ValueError):
        manager.update({"worker.job_timeout": 30.0, "worker.search_timeout": 30.0})
    with pytest.raises(ValueError):
        manager.update({"ai.enabled": "yes"})
    assert manager.snapshot() == {}
    assert not (tmp_path / "admin-state.json").exists()


def test_a_blank_secret_keeps_the_stored_one_and_null_clears_it(tmp_path):
    manager = ConfigManager(settings_for(tmp_path))
    manager.update({"ai.api_key": "k1"})
    manager.update({"ai.api_key": ""})
    assert manager.snapshot() == {"ai.api_key": "k1"}
    manager.update({"ai.api_key": None})
    assert manager.snapshot() == {}


def test_a_stored_layer_is_in_force_after_a_restart(tmp_path):
    store = AdminStateStore(tmp_path / "admin-state.json")
    store.save(credentials={"user_id": "admin"}, sources=[], bots=[],
               settings={"ai.enabled": True, "ai.model": "m", "ai.api_key": "k",
                         "wecom.allowed_users": ["one", "two"]})
    settings = settings_for(tmp_path)
    app = prepared_app(settings)
    assert settings.ai.enabled is True
    assert settings.ai.model == "m"
    assert settings.wecom.allowed_users == ["one", "two"]

    async def scenario():
        async with client_for(app) as client:
            await client.post("/admin/login", json=OPERATOR_LOGIN)
            return await client.get("/admin/config")

    response = run(scenario())
    assert response.status_code == 200
    fields = fields_of(response.json())
    assert fields["ai.model"]["source"] == "panel"
    # The stored key is reported as set, never as its own value.
    assert fields["ai.api_key"]["set"] is True
    assert "k" != fields["ai.api_key"]["value"]


def test_a_stored_value_that_no_longer_validates_is_dropped_without_stopping_the_app(tmp_path):
    store = AdminStateStore(tmp_path / "admin-state.json")
    store.save(credentials={}, sources=[], bots=[],
               settings={"ai.model": "kept", "worker.max_attempts": 0, "nope.key": "x"})
    settings = settings_for(tmp_path)
    app = create_app(settings)
    assert settings.ai.model == "kept"
    assert settings.worker.max_attempts == 3
    payload = app.state.admin.config.describe()
    assert "worker.max_attempts" in payload["rejected"]
    assert "nope.key" in payload["rejected"]
    assert "ai.model" not in payload["rejected"]


def test_a_write_survives_a_restart_through_the_api(tmp_path):
    async def write():
        app = prepared_app(settings_for(tmp_path))
        async with client_for(app) as client:
            token = (await client.post("/admin/login", json=OPERATOR_LOGIN)).json()["csrf_token"]
            return await client.patch("/admin/config",
                                      json={"values": {"worker.max_attempts": 7,
                                                       "wecom.allowed_users": ["alice"]}},
                                      headers={"x-csrf-token": token})

    assert run(write()).status_code == 200
    document = json.loads((tmp_path / "admin-state.json").read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert document["settings"] == {"worker.max_attempts": 7, "wecom.allowed_users": ["alice"]}

    restarted = settings_for(tmp_path)
    create_app(restarted)
    assert restarted.worker.max_attempts == 7
    assert restarted.wecom.allowed_users == ["alice"]


def test_the_wecom_message_proxy_is_editable_and_a_blank_means_the_official_api(tmp_path):
    manager = ConfigManager(settings_for(tmp_path))
    field = fields_of(manager.describe())["wecom.api_base"]
    assert field["env"] == "MUSICDL_WECOM__API_BASE"
    assert field["scope"] == "hot"
    assert field["secret"] is False
    assert str(field["value"]).rstrip("/") == "https://qyapi.weixin.qq.com"

    manager.update({"wecom.api_base": "http://115.159.107.211:9080"})
    assert str(manager.settings.wecom.api_base).rstrip("/") == "http://115.159.107.211:9080"

    # Clearing the box asks for the official endpoint rather than failing the
    # write: an operator who removes the proxy must not be locked out.
    manager.update({"wecom.api_base": ""})
    assert str(manager.settings.wecom.api_base).rstrip("/") == "https://qyapi.weixin.qq.com"

    with pytest.raises(ValueError):
        manager.update({"wecom.api_base": "115.159.107.211:9080"})


def test_config_routes_need_a_session_and_a_csrf_token(tmp_path):
    app = prepared_app(settings_for(tmp_path))

    async def scenario():
        async with client_for(app) as client:
            anonymous = await client.get("/admin/config")
            login = await client.post("/admin/login", json=OPERATOR_LOGIN)
            unguarded = await client.patch("/admin/config", json={"values": {"ai.model": "m"}})
            return anonymous, unguarded, login

    anonymous, unguarded, login = run(scenario())
    assert login.status_code == 200
    assert anonymous.status_code == 401
    assert unguarded.status_code == 403

    async def accept():
        async with client_for(app) as client:
            token = (await client.post("/admin/login", json=OPERATOR_LOGIN)).json()["csrf_token"]
            return await client.patch("/admin/config", json={"values": {"ai.model": "m"}},
                                      headers={"x-csrf-token": token})

    assert run(accept()).status_code == 200


def test_a_hot_setting_changes_the_login_limit_without_a_restart(tmp_path):
    app = prepared_app(settings_for(tmp_path))

    async def scenario():
        async with client_for(app) as client:
            token = (await client.post("/admin/login", json=OPERATOR_LOGIN)).json()["csrf_token"]
            wrong = {"username": OPERATOR_LOGIN["username"], "password": "nope"}
            before = [(await client.post("/admin/login", json=wrong)).status_code for _ in range(2)]
            patched = await client.patch("/admin/config", json={"values": {"admin.login_limit": 1}},
                                         headers={"x-csrf-token": token})
            after = (await client.post("/admin/login", json=wrong)).status_code
        return before, patched.status_code, after

    before, patched, after = run(scenario())
    assert before == [401, 401]
    assert patched == 200
    assert after == 429
