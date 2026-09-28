import asyncio

from test_admin_search_download import build, client_for, signed_in, first_candidate


def test_probe_requires_session_and_csrf(tmp_path):
    async def scenario():
        app, _, _ = build(tmp_path)
        async with client_for(app) as client:
            assert (await client.post("/admin/search/probe", json={"candidates": []})).status_code == 403
            await signed_in(client)
            assert (await client.post("/admin/search/probe", json={"candidates": []})).status_code == 403
    asyncio.run(scenario())


def test_probe_limit_and_unsupported_source(tmp_path):
    async def scenario():
        app, source, _ = build(tmp_path)
        async with client_for(app) as client:
            token = await signed_in(client)
            candidate = await first_candidate(client)
            headers = {"x-csrf-token": token}
            assert (await client.post("/admin/search/probe", json={"candidates": [candidate] * 11}, headers=headers)).status_code == 400
            response = await client.post("/admin/search/probe", json={"candidates": [candidate]}, headers=headers)
            assert response.status_code == 200
            assert response.json() == {candidate["source_id"]: {"1": {"size": None, "extension": None, "media_type": None, "quality": None}}}
            assert source.downloaded == []
    asyncio.run(scenario())


def test_probe_endpoint_returns_success_without_download(tmp_path):
    from fastapi import FastAPI
    from musicdl.admin.auth import AdminAuth
    from musicdl.admin.portal import create_admin_router
    from test_media_probe import make_source
    from types import SimpleNamespace
    async def scenario():
        source, candidate = make_source(tmp_path)
        auth = AdminAuth()
        auth.change_credentials("admin", "operator", "new-password")
        app = FastAPI()
        app.include_router(create_admin_router(auth=auth, runtime=lambda: SimpleNamespace(resolvers={"demo": source}, registry=object())))
        async with client_for(app) as client:
            assert (await client.post("/admin/search/probe", json={"candidates": []})).status_code == 401
            token = await signed_in(client)
            response = await client.post("/admin/search/probe", json={"candidates": [candidate.public_representation]}, headers={"x-csrf-token": token})
            assert response.status_code == 200
            assert response.json() == {"demo": {"1": {"size": 22, "extension": "mp3", "media_type": "audio/mpeg", "quality": None}}}
    asyncio.run(scenario())


def test_invalid_probe_payload(tmp_path):
    async def scenario():
        app, _, _ = build(tmp_path)
        async with client_for(app) as client:
            token = await signed_in(client)
            for body in ({}, {"candidates": "x"}, {"candidates": [{}]}):
                response = await client.post("/admin/search/probe", json=body, headers={"x-csrf-token": token})
                assert response.status_code == 400
                assert response.json()["detail"] == "invalid_probe_request"
    asyncio.run(scenario())
