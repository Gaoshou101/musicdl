"""Durable administrator sessions, remember-me cookies, and bounded auth work."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import threading

import httpx
import pytest
from fastapi import FastAPI

from musicdl.admin import auth as auth_module
from musicdl.admin.auth import AdminAuth, PasswordHasher, RateLimiter
from musicdl.admin.portal import create_admin_router
from musicdl.admin.sessions import (REMEMBERED_SESSION_TTL_SECONDS,
                                    SESSION_TTL_SECONDS, SessionCapacityError)
from musicdl.admin.store import AdminStateStore
from musicdl.app import create_app
from musicdl.config import AppSettings

OPERATOR = {"username": "operator", "password": "correct-horse-battery"}


def settings_for(tmp_path) -> AppSettings:
    settings = AppSettings()
    settings.admin.state_path = str(tmp_path / "admin-state.json")
    return settings


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


def run(coro):
    return asyncio.run(coro)


def test_session_ttls_expire_and_capacity_reclaims_expired_records():
    now = [1_000.0]
    auth = AdminAuth(hasher=PasswordHasher(iterations=100_000), session_capacity=1,
                     session_clock=lambda: now[0])
    regular = auth.issue_session()
    assert auth.session_user(regular) == "admin"
    with pytest.raises(SessionCapacityError):
        auth.issue_session(remember=True)

    now[0] += SESSION_TTL_SECONDS
    assert auth.session_user(regular) is None
    remembered = auth.issue_session(remember=True)
    now[0] += SESSION_TTL_SECONDS + 1
    assert auth.session_user(remembered) == "admin"
    now[0] += REMEMBERED_SESSION_TTL_SECONDS
    assert auth.session_user(remembered) is None


def test_remembered_session_restores_csrf_and_logout_survives_restart(tmp_path):
    async def scenario():
        first = create_app(settings_for(tmp_path))
        first.state.admin.auth.change_credentials("admin", OPERATOR["username"], OPERATOR["password"])
        async with client_for(first) as client:
            login = await client.post("/admin/login", json={**OPERATOR, "remember": True})
            assert login.status_code == 200
            session_token = client.cookies.get("admin_session")
            csrf = login.json()["csrf_token"]
            cookie = login.headers["set-cookie"]
            assert f"Max-Age={REMEMBERED_SESSION_TTL_SECONDS}" in cookie
            assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" in cookie

        state_path = tmp_path / "admin-state.json"
        persisted = state_path.read_text(encoding="utf-8")
        document = json.loads(persisted)
        row = document["sessions"][0]
        assert row["token_hash"] == hashlib.sha256(session_token.encode()).hexdigest()
        assert row["csrf_hash"] == hashlib.sha256(csrf.encode()).hexdigest()
        assert session_token not in persisted and csrf not in persisted

        second = create_app(settings_for(tmp_path))
        async with client_for(second) as client:
            client.cookies.set("admin_session", session_token)
            client.cookies.set("csrf_token", csrf)
            restored = await client.get("/admin/session")
            assert restored.json() == {"authenticated": True, "user_id": "operator",
                                       "must_change": False, "csrf_token": csrf}
            changed = await client.patch("/admin/config", json={"values": {"worker.max_attempts": 4}},
                                         headers={"x-csrf-token": csrf})
            assert changed.status_code == 200
            logout = await client.post("/admin/logout", headers={"x-csrf-token": csrf})
            assert logout.status_code == 200 and logout.json() == {"ok": True}

        third = create_app(settings_for(tmp_path))
        async with client_for(third) as client:
            client.cookies.set("admin_session", session_token)
            client.cookies.set("csrf_token", csrf)
            revoked = await client.get("/admin/session")
        return restored, changed, logout, revoked

    restored, changed, logout, revoked = run(scenario())
    assert restored.status_code == changed.status_code == logout.status_code == revoked.status_code == 200
    assert revoked.json() == {"authenticated": False}


def test_credential_change_revokes_old_sessions_after_restart(tmp_path):
    settings = settings_for(tmp_path)
    app = create_app(settings)
    app.state.admin.auth.change_credentials("admin", OPERATOR["username"], OPERATOR["password"])

    async def login():
        async with client_for(app) as client:
            response = await client.post("/admin/login", json={**OPERATOR, "remember": True})
            assert response.status_code == 200
            return client.cookies.get("admin_session"), response.json()["csrf_token"]

    old_session, old_csrf = run(login())
    app.state.admin.auth.change_credentials(OPERATOR["username"], "operator", "replacement-password")
    restarted = create_app(settings_for(tmp_path))

    async def check():
        async with client_for(restarted) as client:
            client.cookies.set("admin_session", old_session)
            client.cookies.set("csrf_token", old_csrf)
            session = await client.get("/admin/session")
            old_password = await client.post("/admin/login", json=OPERATOR)
            new_password = await client.post("/admin/login", json={"username": "operator",
                                                                    "password": "replacement-password"})
            return session, old_password, new_password

    session, old_password, new_password = run(check())
    assert session.json() == {"authenticated": False}
    assert old_password.status_code == 401
    assert new_password.status_code == 200


def test_legacy_credential_state_without_version_still_loads(tmp_path):
    store = AdminStateStore(tmp_path / "admin-state.json")
    encoded = PasswordHasher(iterations=100_000).hash("legacy-password")
    store.save(credentials={"user_id": "legacy-admin", "password_hash": encoded,
                            "must_change": False}, sources=[], bots=[], settings={})

    app = create_app(settings_for(tmp_path))
    assert app.state.admin.auth.authenticate("legacy-admin", "legacy-password").ok
    assert app.state.admin.auth.snapshot()["credential_version"] == 1


def test_failed_credential_persist_restores_credentials_and_sessions():
    should_fail = [False]

    def persist():
        if should_fail[0]:
            raise OSError("state write failed")

    auth = AdminAuth(hasher=PasswordHasher(iterations=100_000), on_change=persist)
    session = auth.issue_session()
    auth.bind_csrf(session, "csrf-value")
    should_fail[0] = True

    with pytest.raises(OSError, match="state write failed"):
        auth.change_credentials("admin", "operator", "replacement-password")

    assert auth.authenticate("admin", "password").ok
    assert auth.session_user(session) == "admin"
    assert auth.valid_csrf(session, "csrf-value")


def test_async_login_verification_runs_off_loop_and_rejects_stale_credentials():
    started = threading.Event()
    release = threading.Event()
    worker_threads: list[int] = []

    class BlockingHasher:
        def hash(self, _password: str) -> str:
            return "test-hash"

        def accepts(self, encoded: str) -> bool:
            return encoded == "test-hash"

        def verify(self, _password: str, _encoded: str) -> bool:
            worker_threads.append(threading.get_ident())
            started.set()
            release.wait(timeout=5)
            return True

    auth = AdminAuth(hasher=BlockingHasher())
    app = FastAPI()
    app.include_router(create_admin_router(auth=auth, limiter=RateLimiter()))

    async def scenario():
        async with client_for(app) as client:
            pending = asyncio.create_task(client.post("/admin/login",
                                                       json={"username": "admin", "password": "password"}))
            assert await asyncio.to_thread(started.wait, 2)
            main_thread = threading.get_ident()
            # The event loop can perform an independent credential change while
            # the old verifier remains blocked in its worker thread.
            auth.change_credentials("admin", "operator", "replacement-password")
            release.set()
            response = await pending
            return response, main_thread

    try:
        response, main_thread = run(scenario())
    finally:
        release.set()
    assert worker_threads and worker_threads[0] != main_thread
    assert response.status_code == 401
    assert "admin_session" not in response.cookies


def test_cancelled_queued_password_work_keeps_executor_capacity_bounded(monkeypatch):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    slots = threading.BoundedSemaphore(4)
    monkeypatch.setattr(auth_module, "_PASSWORD_EXECUTOR", executor)
    monkeypatch.setattr(auth_module, "_PASSWORD_WORK_SLOTS", slots)

    started = [threading.Event() for _ in range(4)]
    both_workers_started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    state_lock = threading.Lock()
    started_count = 0
    finished_count = 0

    def password_work(number):
        nonlocal started_count, finished_count
        with state_lock:
            started_count += 1
            if started_count == 2:
                both_workers_started.set()
        started[number].set()
        if not release.wait(timeout=5):
            raise TimeoutError("test password work was not released")
        with state_lock:
            finished_count += 1
            if finished_count == 4:
                finished.set()
        return number

    async def scenario():
        accepted = [asyncio.create_task(auth_module._run_password_work(password_work, number))
                    for number in range(4)]
        try:
            assert await asyncio.to_thread(both_workers_started.wait, 2)
            assert executor._work_queue.qsize() == 2
            assert slots._value == 0

            # These request tasks are waiting on executor-queued operations.
            # Canceling their asyncio waiters must leave both native work items
            # in the bounded queue and their permits reserved.
            accepted[2].cancel()
            accepted[3].cancel()
            cancelled = await asyncio.gather(*accepted[2:], return_exceptions=True)
            assert all(isinstance(result, asyncio.CancelledError) for result in cancelled)
            assert executor._work_queue.qsize() == 2
            assert slots._value == 0

            # Churn cannot turn those still-queued native operations into free
            # capacity for additional work.
            for _ in range(20):
                with pytest.raises(auth_module.AuthBusyError):
                    await auth_module._run_password_work(password_work, 0)
            assert executor._work_queue.qsize() == 2

            release.set()
            assert await asyncio.gather(*accepted[:2]) == [0, 1]
            assert await asyncio.to_thread(finished.wait, 2)
            deadline = asyncio.get_running_loop().time() + 2
            while slots._value != 4 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.001)
            assert slots._value == 4
        finally:
            release.set()
            await asyncio.gather(*accepted, return_exceptions=True)
            executor.shutdown(wait=True)

    run(scenario())
