import asyncio

import httpx


def get(path, route):
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=path), base_url="http://test") as client:
            return await client.get(route)

    return asyncio.run(request())


def test_main_health_is_dependency_free():
    from musicdl.app import app

    response = get(app, "/healthz")
    assert response.status_code == 200
    assert response.json() == {"service": "musicdl", "status": "ok", "config_version": 1}


def test_plugin_runner_health_exposes_protocol():
    from musicdl_plugin_runner.app import app

    response = get(app, "/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "service": "plugin-runner",
        "status": "ok",
        "protocol": "musicdl.plugin/v1",
    }
