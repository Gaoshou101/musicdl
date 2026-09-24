"""A direct HTTP deployment can finish first-login setup without a proxy."""

import asyncio
from http.cookies import SimpleCookie
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

from musicdl.admin.config import ConfigManager
from musicdl.app import create_app
from musicdl.config import AppSettings


@pytest.mark.parametrize("mode", ["json", "form"])
@pytest.mark.parametrize("secure,base_url", [
    (False, "http://192.0.2.1"),
    (True, "https://admin.example.test"),
])
def test_login_and_password_change_keep_a_usable_session(monkeypatch, mode, secure, base_url):
    monkeypatch.setenv("MUSICDL_ADMIN__COOKIE_SECURE", str(secure).lower())
    app = create_app(AppSettings())
    suffix = "-form" if mode == "form" else ""
    expected_status = 303 if mode == "form" else 200

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url) as client:
            login_data = {"username": "admin", "password": "password"}
            login = await client.post("/admin/login" + suffix,
                                      **{("data" if mode == "form" else "json"): login_data})
            assert login.status_code == expected_status
            old_session = client.cookies.get("admin_session")
            old_csrf = client.cookies.get("csrf_token")
            assert (await client.get("/admin/sources")).status_code == 403

            change_data = {"username": "operator", "password": "password",
                           "new_password": "correct-horse-battery"}

            async def change(token):
                if mode == "form":
                    return await client.post("/admin/change-credentials-form",
                                             data={**change_data, "csrf_token": token})
                return await client.post("/admin/change-credentials", json=change_data,
                                         headers={"x-csrf-token": token})

            assert (await change("forged")).status_code == 403
            changed = await change(old_csrf)
            assert changed.status_code == expected_status
            assert client.cookies.get("admin_session") != old_session
            assert client.cookies.get("csrf_token") != old_csrf
            assert (await client.get("/admin/sources")).status_code == 200
            stale = await client.get("/admin/sources", headers={"cookie": f"admin_session={old_session}"})
            assert stale.status_code == 401

            for response in (login, changed):
                cookies = SimpleCookie()
                for value in response.headers.get_list("set-cookie"):
                    cookies.load(value)
                assert set(cookies) == {"admin_session", "csrf_token"}
                assert bool(cookies["admin_session"]["httponly"])
                assert not cookies["csrf_token"]["httponly"]
                for cookie in cookies.values():
                    assert bool(cookie["secure"]) is secure
                    assert cookie["samesite"] == "lax"

            assert (await client.post("/admin/login", json=login_data)).status_code == 401
            fresh = await client.post("/admin/login", json={"username": "operator",
                                                           "password": change_data["new_password"]})
            assert fresh.status_code == 200 and fresh.json()["must_change"] is False
            assert (await client.get("/admin/sources")).status_code == 200

    asyncio.run(scenario())


@pytest.mark.parametrize("forwarded_proto", ["http", "https", "http, https"])
def test_default_secure_cookie_cannot_be_downgraded_by_forwarded_header(monkeypatch, forwarded_proto):
    monkeypatch.delenv("MUSICDL_ADMIN__COOKIE_SECURE", raising=False)
    app = create_app(AppSettings())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://192.0.2.1") as client:
            response = await client.post("/admin/login", json={"username": "admin", "password": "password"},
                                         headers={"x-forwarded-proto": forwarded_proto})
            assert response.status_code == 200
            assert all("Secure" in value for value in response.headers.get_list("set-cookie"))
            assert (await client.get("/admin/sources")).status_code == 401

    asyncio.run(scenario())


def test_cookie_policy_is_deployment_owned_and_survives_panel_updates(monkeypatch):
    monkeypatch.setenv("MUSICDL_ADMIN__COOKIE_SECURE", "false")
    settings = AppSettings()
    assert settings.admin.cookie_secure is False
    manager = ConfigManager(settings)
    manager.update({"admin.login_limit": 10})
    assert settings.admin.cookie_secure is False
    with pytest.raises(ValueError, match="unknown setting"):
        manager.update({"admin.cookie_secure": True})


def test_invalid_cookie_policy_fails_startup(monkeypatch):
    monkeypatch.setenv("MUSICDL_ADMIN__COOKIE_SECURE", "not-a-boolean")
    with pytest.raises(ValidationError):
        AppSettings()


@pytest.mark.parametrize("filename", ["compose.yaml", "compose.prod.yaml"])
def test_compose_forwards_cookie_policy_to_main_service_only(filename):
    root = Path(__file__).resolve().parents[2]
    services = yaml.safe_load((root / filename).read_text(encoding="utf-8"))["services"]
    variable = "MUSICDL_ADMIN__COOKIE_SECURE"
    assert services["musicdl"]["environment"][variable] == "${MUSICDL_ADMIN__COOKIE_SECURE:-true}"
    assert variable not in services["plugin-runner"].get("environment", {})
