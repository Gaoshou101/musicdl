from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from musicdl.admin.auth import PasswordHasher
from musicdl.admin.config import ConfigManager
from musicdl.app import create_app
from musicdl.config import AppSettings, WeComSettings


def enabled_wecom() -> WeComSettings:
    aes_key = base64.b64encode(b"k" * 32).decode().rstrip("=")
    return WeComSettings(
        enabled=True,
        corp_id="corp",
        agent_id=7,
        token=SecretStr("token"),
        secret=SecretStr("secret"),
        encoding_aes_key=SecretStr(aes_key),
        allowed_users=["user"],
    )


def run(coro):
    return asyncio.run(coro)


def test_full_mode_remains_the_default_and_keeps_wecom_available(monkeypatch):
    monkeypatch.delenv("MUSICDL_DEPLOYMENT_MODE", raising=False)

    settings = AppSettings(wecom=enabled_wecom())

    assert settings.deployment_mode == "full"
    assert settings.wecom.enabled is True


def test_lite_mode_maps_from_environment_and_rejects_wecom_at_startup(monkeypatch):
    monkeypatch.setenv("MUSICDL_DEPLOYMENT_MODE", "lite")

    settings = AppSettings()

    assert settings.deployment_mode == "lite"
    assert settings.wecom.enabled is False
    with pytest.raises(ValidationError, match="WeCom is not available in lite deployment mode"):
        AppSettings(wecom=enabled_wecom())


def test_lite_mode_rejects_saved_wecom_before_changing_state_file_or_credentials(tmp_path):
    state_path = tmp_path / "admin-state.json"
    credentials = {
        "user_id": "operator",
        "password_hash": PasswordHasher(iterations=100_000).hash("correct-horse-battery"),
        "must_change": False,
    }
    original = json.dumps(
        {
            "version": 1,
            "credentials": credentials,
            "sources": [],
            "bots": [],
            "settings": {"wecom.enabled": True},
        },
        indent=2,
    ).encode("utf-8")
    state_path.write_bytes(original)
    settings = AppSettings(deployment_mode="lite")
    # The deployment path is POSIX-only in the model; direct assignment lets
    # this store test use the platform's temporary directory.
    settings.admin.state_path = str(state_path)

    with pytest.raises(ValueError, match="WeCom is not available in lite deployment mode"):
        create_app(settings)

    assert state_path.read_bytes() == original
    assert json.loads(state_path.read_bytes())["credentials"] == credentials


def test_lite_mode_load_preflight_does_not_mutate_or_recover_saved_override():
    settings = AppSettings(deployment_mode="lite")
    manager = ConfigManager(settings)

    with pytest.raises(ValueError, match="WeCom is not available in lite deployment mode"):
        manager.load({"settings": {"wecom.enabled": True}})

    assert manager.snapshot() == {}
    assert manager.describe()["rejected"] == {}
    assert settings.wecom.enabled is False


def test_lite_mode_rejects_hot_batch_before_adopt_or_persist(tmp_path):
    settings = AppSettings(deployment_mode="lite")
    callbacks: list[str] = []
    manager = ConfigManager(
        settings,
        on_change=lambda: callbacks.append("persist"),
        on_adopt=lambda: callbacks.append("adopt"),
    )

    with pytest.raises(ValueError, match="WeCom is not available in lite deployment mode"):
        manager.update({"ai.model": "must-not-apply", "wecom.enabled": True})

    assert callbacks == []
    assert manager.snapshot() == {}
    assert settings.ai.model is None
    assert settings.wecom.enabled is False
    assert not (tmp_path / "admin-state.json").exists()


def test_lite_mode_hot_batch_is_reported_as_http_422_without_persisting(tmp_path):
    settings = AppSettings(deployment_mode="lite")
    state_path = tmp_path / "admin-state.json"
    settings.admin.state_path = str(state_path)
    app = create_app(settings)
    app.state.admin.auth.change_credentials("admin", "operator", "correct-horse-battery")
    original = state_path.read_bytes()

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://test"
        ) as client:
            login = await client.post(
                "/admin/login",
                json={"username": "operator", "password": "correct-horse-battery"},
            )
            token = login.json()["csrf_token"]
            response = await client.patch(
                "/admin/config",
                json={"values": {"ai.model": "must-not-apply", "wecom.enabled": True}},
                headers={"x-csrf-token": token},
            )
            return login, response

    login, response = run(scenario())

    assert login.status_code == 200
    assert response.status_code == 422
    assert response.json()["detail"] == "WeCom is not available in lite deployment mode"
    assert state_path.read_bytes() == original
    assert app.state.admin.config.snapshot() == {}
    assert settings.ai.model is None


def test_panel_cannot_change_deployment_mode():
    manager = ConfigManager(AppSettings(deployment_mode="lite"))

    with pytest.raises(ValueError, match="unknown setting: deployment_mode"):
        manager.update({"deployment_mode": "full"})

    assert manager.settings.deployment_mode == "lite"
    assert manager.snapshot() == {}


def test_valid_lite_settings_are_retained_by_config_materialization(monkeypatch):
    monkeypatch.setenv("MUSICDL_DEPLOYMENT_MODE", "lite")
    settings = AppSettings()
    manager = ConfigManager(settings)

    manager.update({"ai.model": "allowed-in-lite"})

    assert settings.deployment_mode == "lite"
    assert manager._materialize(manager.snapshot()).deployment_mode == "lite"
    assert settings.ai.model == "allowed-in-lite"
    assert settings.wecom.enabled is False
