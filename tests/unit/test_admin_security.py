import asyncio
import httpx
import pytest
from fastapi import FastAPI

from musicdl.admin.auth import AdminAuth, PasswordHasher, RateLimiter
from musicdl.admin.csrf import CSRFMiddleware
from musicdl.admin.portal import create_admin_router


def test_password_hash_is_salted_and_verifiable():
    hasher = PasswordHasher()
    encoded = hasher.hash("secret")
    assert encoded != "secret"
    assert hasher.verify("secret", encoded)
    assert not hasher.verify("wrong", encoded)
    assert hasher.hash("secret") != encoded


def test_default_credentials_require_change_and_old_password_expires():
    auth = AdminAuth()
    result = auth.authenticate("admin", "password")
    assert result.ok and result.must_change
    auth.change_credentials(result.user_id, "operator", "new-password")
    assert not auth.authenticate("admin", "password").ok
    assert auth.authenticate("operator", "new-password").ok


def test_rate_limiter_blocks_after_limit_and_resets():
    now = [100.0]
    limiter = RateLimiter(limit=2, window_seconds=60, clock=lambda: now[0])
    assert limiter.allow("ip")
    assert limiter.allow("ip")
    assert not limiter.allow("ip")
    now[0] += 61
    assert limiter.allow("ip")


def test_session_cookie_and_csrf_are_enforced():
    app = FastAPI()
    auth = AdminAuth(); session = auth.issue_session(); auth.bind_csrf(session, "token")
    app.add_middleware(CSRFMiddleware, auth=auth)

    @app.post("/change")
    async def change():
        return {"ok": True}

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            client.cookies.set("csrf_token", "token")
            client.cookies.set("admin_session", session)
            return await client.post("/change"), await client.post("/change", headers={"X-CSRF-Token": "token"})
    response, response2 = asyncio.run(run())
    assert response.status_code == 403
    assert response2.status_code == 200


def test_login_sets_secure_http_only_samesite_session_cookie():
    app = FastAPI()
    app.include_router(create_admin_router())
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/admin/login", json={"username": "admin", "password": "password"})
    response = asyncio.run(run())
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "admin_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" in cookie


def test_csrf_allows_login_bootstrap_endpoint():
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    @app.post("/admin/login")
    async def login():
        return {"ok": True}
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/admin/login")
    assert asyncio.run(run()).status_code == 200


def test_password_hash_rejects_cpu_dos_iteration_counts_and_weak_passwords():
    h = PasswordHasher()
    assert not h.verify("x", "pbkdf2_sha256$999999999$YWJj$YWJj")
    auth = AdminAuth()
    with pytest.raises(ValueError): auth.change_credentials("admin", "x", "short")
    with pytest.raises(ValueError): auth.change_credentials("admin", "x", "password")


def test_csrf_middleware_rejects_unbound_and_cross_session_tokens():
    auth = AdminAuth(); s1 = auth.issue_session(); s2 = auth.issue_session(); auth.bind_csrf(s1, "one"); auth.bind_csrf(s2, "two")
    app = FastAPI(); app.add_middleware(CSRFMiddleware, auth=auth)
    @app.post("/change")
    async def change(): return {"ok": True}
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            c.cookies.set("admin_session", s1); c.cookies.set("csrf_token", "fake")
            a = await c.post("/change", headers={"x-csrf-token":"fake"})
            c.cookies.set("csrf_token", "two")
            b = await c.post("/change", headers={"x-csrf-token":"two"})
            return a,b
    a,b=asyncio.run(run()); assert a.status_code == 403 and b.status_code == 403
