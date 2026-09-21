"""The console is served by the process it manages, from files the image bakes in.

A deployment used to run Next as a second container and forward its `/api/*`
calls to the app. The console is a client-only build, so it ships as static
files now and these tests hold the two properties that replaced that hop: one
origin serves the files, and every route the app already owned is decided before
any of them is consulted.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from musicdl.app import create_app
from musicdl.config import AppSettings


def panel_tree(root):
    """The shape `next build` produces with `trailingSlash`: a directory per route."""
    (root / "dashboard" / "config").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>控制台</title>", encoding="utf-8")
    (root / "dashboard" / "config" / "index.html").write_text("<title>运行配置</title>", encoding="utf-8")
    return root


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


def run(coro):
    return asyncio.run(coro)


def test_the_app_serves_the_console_and_still_owns_every_route_above_it(tmp_path):
    app = create_app(AppSettings(admin={"panel_root": str(panel_tree(tmp_path / "panel"))}))

    async def scenario():
        async with client_for(app) as client:
            return {
                "home": await client.get("/"),
                "deep": await client.get("/dashboard/config/"),
                "bare_deep": await client.get("/dashboard/config"),
                "followed": await client.get("/dashboard/config", follow_redirects=True),
                "health": await client.get("/healthz"),
                "api": await client.get("/admin/sources"),
                "login_page": await client.get("/admin/"),
                "unclaimed": await client.get("/no-such-page"),
            }

    seen = run(scenario())
    assert seen["home"].status_code == 200 and "控制台" in seen["home"].text
    # A deep link is a directory with an index file in it, which is what a plain
    # file server can answer; without the trailing slash it says so and redirects
    # rather than 404-ing a URL an operator typed.
    assert seen["deep"].status_code == 200 and "运行配置" in seen["deep"].text
    assert seen["bare_deep"].status_code == 307
    assert seen["followed"].status_code == 200 and "运行配置" in seen["followed"].text
    # Everything below was registered before the panel mount, so the console can
    # never shadow the service's own endpoints -- not even with its 404 page.
    assert seen["health"].status_code == 200 and seen["health"].json()["status"] == "ok"
    assert seen["api"].status_code == 401
    assert seen["login_page"].status_code == 200 and 'action="/admin/login-form"' in seen["login_page"].text
    assert seen["unclaimed"].status_code == 404


def test_a_deployment_without_a_console_serves_no_front_page():
    app = create_app(AppSettings())

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/"), await client.get("/healthz")

    home, health = run(scenario())
    assert home.status_code == 404
    assert health.status_code == 200


def test_a_console_directory_that_is_not_there_is_reported_and_not_fatal(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="musicdl.app"):
        app = create_app(AppSettings(admin={"panel_root": str(tmp_path / "absent")}))

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/"), await client.get("/healthz")

    home, health = run(scenario())
    assert home.status_code == 404
    assert health.status_code == 200
    assert "does not exist" in caplog.text