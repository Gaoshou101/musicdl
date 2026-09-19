"""The panel's window onto the service's own log records.

The two stores beside this one answer questions the portal asked; this route
answers what the process printed, which is what an operator has to read when a
download failed for a reason no event carries.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import FastAPI

from musicdl.admin.auth import AdminAuth
from musicdl.admin.csrf import CSRFMiddleware
from musicdl.admin.logs import LogBuffer
from musicdl.admin.portal import create_admin_router

LOGIN = {"username": "operator", "password": "new-password"}


def build(buffer: LogBuffer | None = None):
    auth = AdminAuth()
    auth.change_credentials("admin", "operator", "new-password")
    buffer = buffer or LogBuffer(capacity=20)
    app = FastAPI()
    app.include_router(create_admin_router(auth=auth, logs=buffer))
    app.add_middleware(CSRFMiddleware, auth=auth)
    return app, buffer


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


async def signed_in(client) -> str:
    response = await client.post("/admin/login", json=LOGIN)
    return response.json()["csrf_token"]


def run(coro):
    return asyncio.run(coro)


def test_the_log_window_requires_a_session():
    app, _ = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/admin/logs")

    assert run(scenario()).status_code == 401


def test_the_window_shows_what_the_service_logged_while_it_ran():
    app, buffer = build()
    buffer.install()
    try:
        logging.getLogger("musicdl.worker").warning("channel %s answered 502", "hywmusic-beta")

        async def scenario():
            async with client_for(app) as client:
                await signed_in(client)
                return await client.get("/admin/logs", params={"level": "warning"})

        response = run(scenario())
    finally:
        buffer.uninstall()

    assert response.status_code == 200
    payload = response.json()
    entry = payload["items"][-1]
    assert entry["logger"] == "musicdl.worker" and entry["level"] == "warning"
    assert entry["message"] == "channel hywmusic-beta answered 502"
    assert entry["id"] == payload["last_id"] and payload["total"] >= 1


def test_the_cursor_returns_only_what_came_after_it():
    app, buffer = build()
    buffer.append(logging.LogRecord("musicdl", logging.INFO, __file__, 1, "first", None, None))

    async def scenario():
        async with client_for(app) as client:
            await signed_in(client)
            head = await client.get("/admin/logs", params={"limit": 5, "level": "info"})
            buffer.append(logging.LogRecord("musicdl", logging.INFO, __file__, 2, "second", None, None))
            tail = await client.get("/admin/logs",
                                    params={"limit": 5, "after": head.json()["last_id"], "level": "info"})
        return head, tail

    head, tail = run(scenario())
    assert [item["message"] for item in head.json()["items"]] == ["first"]
    assert [item["message"] for item in tail.json()["items"]] == ["second"]


def test_the_window_refuses_parameters_it_cannot_answer():
    app, _ = build()

    async def scenario():
        async with client_for(app) as client:
            await signed_in(client)
            return [await client.get("/admin/logs", params=params) for params in
                    ({"limit": 0}, {"limit": 501}, {"after": -1}, {"level": "verbose"})]

    for response in run(scenario()):
        assert response.status_code == 422


def test_a_rollup_without_a_configured_handler_still_answers():
    """The router builds its own buffer when the application hands it none."""
    auth = AdminAuth()
    auth.change_credentials("admin", "operator", "new-password")
    app = FastAPI()
    app.include_router(create_admin_router(auth=auth))
    app.add_middleware(CSRFMiddleware, auth=auth)

    async def scenario():
        async with client_for(app) as client:
            await signed_in(client)
            return await client.get("/admin/logs")

    response = run(scenario())
    assert response.status_code == 200 and response.json()["items"] == []
