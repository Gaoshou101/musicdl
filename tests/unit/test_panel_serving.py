"""The console is served by the process it manages, from files the image bakes in.

The Vue console ships as static files with an explicit history-route allowlist.
API routes, missing assets and unknown pages must never receive the SPA entry.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from musicdl.app import create_app
from musicdl.config import AppSettings


def panel_tree(root):
    """Vite emits one entry document and content-hashed asset files."""
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>控制台</title>", encoding="utf-8")
    (root / "assets" / "panel-123.js").write_text("console.log('panel')", encoding="utf-8")
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
                "search": await client.get("/dashboard/search/?q=hello%20world"),
                "asset": await client.get("/assets/panel-123.js"),
                "missing_asset": await client.get("/assets/missing.js"),
                "missing_api": await client.get("/admin/not-a-route"),
                "missing_page": await client.get("/dashboard/not-a-page"),
                "health": await client.get("/healthz"),
                "api": await client.get("/admin/sources"),
                "login_page": await client.get("/admin/"),
                "unclaimed": await client.get("/no-such-page"),
            }

    seen = run(scenario())
    assert seen["home"].status_code == 200 and "控制台" in seen["home"].text
    # Both old trailing-slash links and the Vue router's URLs use the SPA entry.
    for name in ("deep", "bare_deep", "search"):
        assert seen[name].status_code == 200 and "控制台" in seen[name].text
        assert seen[name].headers["cache-control"] == "no-cache"
    assert seen["home"].headers["cache-control"] == "no-cache"
    assert seen["asset"].status_code == 200 and "console.log" in seen["asset"].text
    assert "javascript" in seen["asset"].headers["content-type"]
    for name in ("missing_asset", "missing_api", "missing_page"):
        assert seen[name].status_code == 404
        assert "控制台" not in seen[name].text
    # Everything below was registered before the panel mount, so the console can
    # never shadow the service's own endpoints -- not even with its 404 page.
    assert seen["health"].status_code == 200 and seen["health"].json()["status"] == "ok"
    assert seen["api"].status_code == 401
    assert seen["login_page"].status_code == 200 and 'action="/admin/login-form"' in seen["login_page"].text
    assert seen["unclaimed"].status_code == 404


def test_every_panel_deep_link_supports_head_and_rejects_post(tmp_path):
    app = create_app(AppSettings(admin={"panel_root": str(panel_tree(tmp_path / "panel"))}))

    async def scenario():
        async with client_for(app) as client:
            for page in ("", "search", "downloads", "sources", "bots", "config", "health", "logs", "settings"):
                path = "/dashboard" + ("/" + page if page else "")
                response = await client.get(path)
                assert response.status_code == 200 and "控制台" in response.text
                head = await client.head(path)
                assert head.status_code == 200 and head.content == b""
                assert head.headers["cache-control"] == "no-cache"
                assert (await client.post(path)).status_code == 405

    run(scenario())


def test_panel_fallback_does_not_expose_files_outside_its_root(tmp_path):
    root = panel_tree(tmp_path / "panel")
    (tmp_path / "outside.txt").write_text("private fixture", encoding="utf-8")
    app = create_app(AppSettings(admin={"panel_root": str(root)}))

    async def scenario():
        async with client_for(app) as client:
            for path in ("/assets/%2e%2e/%2e%2e/outside.txt", "/dashboard/missing.js", "/api/missing"):
                response = await client.get(path)
                assert response.status_code == 404
                assert "private fixture" not in response.text

    run(scenario())


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
