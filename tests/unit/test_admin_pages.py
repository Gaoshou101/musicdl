"""The panel a browser can actually use: its login page and its forms."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI

from musicdl.admin.auth import AdminAuth, RateLimiter
from musicdl.admin.csrf import CSRFMiddleware
from musicdl.admin.health import EventLogStore
from musicdl.admin.portal import create_admin_router

DEFAULT_LOGIN = {"username": "admin", "password": "password"}
CHANGED_LOGIN = {"username": "operator", "password": "correct-horse-battery"}


def build(*, limiter: RateLimiter | None = None):
    auth, audit = AdminAuth(), EventLogStore()
    app = FastAPI()
    app.include_router(create_admin_router(auth=auth, audit=audit, limiter=limiter or RateLimiter()))
    app.add_middleware(CSRFMiddleware, auth=auth)
    return app, auth, audit


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


def run(coro):
    return asyncio.run(coro)


def test_an_unauthenticated_browser_gets_the_login_page_and_the_api_still_gets_json():
    async def scenario():
        app, _, _ = build()
        async with client_for(app) as client:
            page = await client.get("/admin/")
            api = await client.get("/admin/sources")
        return page, api

    page, api = run(scenario())
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert 'action="/admin/login-form"' in page.text
    assert 'name="username"' in page.text and 'name="password"' in page.text
    assert api.status_code == 401
    assert api.headers["content-type"].startswith("application/json")


def test_the_login_form_issues_a_session_and_redirects_to_the_dashboard():
    async def scenario():
        app, _, _ = build()
        async with client_for(app) as client:
            response = await client.post("/admin/login-form", data=DEFAULT_LOGIN)
            session, csrf = client.cookies.get("admin_session"), client.cookies.get("csrf_token")
            dashboard = await client.get("/admin/")
        return response, session, csrf, dashboard

    response, session, csrf, dashboard = run(scenario())
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/"
    assert session and csrf
    assert dashboard.status_code == 200 and "SECURITY WARNING" in dashboard.text


def test_a_refused_login_answers_with_the_page_and_is_audited():
    async def scenario():
        app, _, audit = build()
        async with client_for(app) as client:
            response = await client.post("/admin/login-form", data={"username": "admin", "password": "wrong"})
        return response, audit.page()

    response, audit = run(scenario())
    assert response.status_code == 401
    assert "用户名或密码不正确" in response.text
    assert audit["items"][-1]["action"] == "login" and audit["items"][-1]["status"] == "failed"


def test_the_login_form_shares_the_login_rate_limit():
    async def scenario():
        app, _, _ = build(limiter=RateLimiter(limit=1))
        async with client_for(app) as client:
            first = await client.post("/admin/login-form", data={"username": "admin", "password": "wrong"})
            second = await client.post("/admin/login-form", data={"username": "admin", "password": "wrong"})
        return first, second

    first, second = run(scenario())
    assert first.status_code == 401
    assert second.status_code == 429 and "登录尝试过于频繁" in second.text


def test_the_dashboard_carries_the_forced_credential_change_form():
    async def scenario():
        app, _, _ = build()
        async with client_for(app) as client:
            await client.post("/admin/login-form", data=DEFAULT_LOGIN)
            return await client.get("/admin/")

    page = run(scenario())
    assert 'action="/admin/change-credentials-form"' in page.text
    assert 'name="csrf_token"' in page.text
    assert 'name="new_password"' in page.text


def test_the_credential_change_form_replaces_the_default_password():
    async def scenario():
        app, auth, _ = build()
        async with client_for(app) as client:
            await client.post("/admin/login-form", data=DEFAULT_LOGIN)
            token = auth.session_csrf(client.cookies.get("admin_session"))
            changed = await client.post("/admin/change-credentials-form",
                                        data={"csrf_token": token, "password": DEFAULT_LOGIN["password"],
                                              "username": CHANGED_LOGIN["username"],
                                              "new_password": CHANGED_LOGIN["password"]})
            dashboard = await client.get("/admin/")
        async with client_for(app) as fresh:
            old = await fresh.post("/admin/login", json=DEFAULT_LOGIN)
            new = await fresh.post("/admin/login", json=CHANGED_LOGIN)
        return changed, dashboard, old, new

    changed, dashboard, old, new = run(scenario())
    assert changed.status_code == 303
    assert dashboard.status_code == 200 and "SECURITY WARNING" not in dashboard.text
    assert old.status_code == 401
    assert new.status_code == 200 and new.json()["must_change"] is False


def test_a_weak_new_password_is_refused_on_the_page_and_keeps_the_session():
    async def scenario():
        app, auth, _ = build()
        async with client_for(app) as client:
            await client.post("/admin/login-form", data=DEFAULT_LOGIN)
            token = auth.session_csrf(client.cookies.get("admin_session"))
            refused = await client.post("/admin/change-credentials-form",
                                        data={"csrf_token": token, "password": "password",
                                              "username": "operator", "new_password": "short"})
            still_valid = await client.post("/admin/login", json=DEFAULT_LOGIN)
        return refused, still_valid

    refused, still_valid = run(scenario())
    assert refused.status_code == 422
    assert "新密码至少 8 位" in refused.text
    assert still_valid.status_code == 200
