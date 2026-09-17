import asyncio
import hashlib

import httpx
from fastapi import FastAPI

from musicdl.admin.auth import AdminAuth
from musicdl.admin.health import EventLogStore
from musicdl.admin.management import SourceManager
from musicdl.admin.portal import create_admin_router
from musicdl.contracts.plugin import MAX_PAYLOAD_BYTES
from musicdl.plugins.store import PluginStore


PYTHON_SOURCE = "def handle(request):\n    return []\n"
LX_SOURCE = """/*
 * @name Demo Source
 * @version v3.2.11
 */
const { EVENT_NAMES, request, on } = globalThis.lx;
const API = 'https://music.example.com/api';
on(EVENT_NAMES.request, ({ info }) => request(`${API}/search?q=${info.keyword}`, { method: 'GET' }));
"""


def _client(tmp_path, *, audit=None, sources=None):
    app = FastAPI()
    store = PluginStore(tmp_path)
    auth = AdminAuth()
    auth.change_credentials("admin", "operator", "new-password")
    app.include_router(create_admin_router(auth=auth, sources=sources or SourceManager(),
                                           audit=audit, plugins=lambda: store))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test"), store


async def _login(client):
    response = await client.post("/admin/login", json={"username": "operator", "password": "new-password"})
    return response.json()["csrf_token"]


def _definition(**overrides):
    body = {"id": "demo", "name": "Demo", "language": "python", "script": PYTHON_SOURCE,
            "allowed_hosts": ["music.example.com"]}
    body.update(overrides)
    return body


def test_creating_a_source_installs_the_script_and_reports_it(tmp_path):
    async def run():
        client, store = _client(tmp_path)
        async with client:
            csrf = await _login(client)
            created = await client.post("/admin/sources", headers={"x-csrf-token": csrf},
                                        json=_definition())
            listing = await client.get("/admin/sources")
        return created, listing, store

    created, listing, store = asyncio.run(run())

    body = created.json()
    assert created.status_code == 200
    assert (body["id"], body["name"], body["enabled"], body["priority"], body["timeout"]) == \
        ("demo", "Demo", True, 0, 10.0)
    digest = hashlib.sha256(PYTHON_SOURCE.encode()).hexdigest()
    assert body["plugin"] == {"sha256": digest, "version": "1", "language": "python",
                              "egress": {"allowed_hosts": ["music.example.com"], "allowed_ports": [443],
                                         "allow_insecure_http": False, "allow_ip_hosts": False,
                                         "allow_any_host": False}}
    assert listing.json()["items"][0]["plugin"]["sha256"] == digest
    assert [item.manifest.plugin_id for item in store.enabled()] == ["demo"]


def test_installing_a_second_script_retires_the_first_version(tmp_path):
    async def run():
        client, store = _client(tmp_path)
        async with client:
            csrf = await _login(client)
            first = await client.post("/admin/sources", headers={"x-csrf-token": csrf}, json=_definition())
            second = await client.post("/admin/sources", headers={"x-csrf-token": csrf},
                                       json=_definition(script="def handle(request):\n    return ()\n"))
        return first, second, store

    first, second, store = asyncio.run(run())

    old, new = first.json()["plugin"]["sha256"], second.json()["plugin"]["sha256"]
    assert old != new
    assert [item.manifest.sha256 for item in store.enabled()] == [new]
    assert store.load("demo", old).enabled is False


def test_a_blocked_lx_source_is_refused_and_leaves_storage_untouched(tmp_path):
    async def run():
        client, store = _client(tmp_path)
        async with client:
            csrf = await _login(client)
            refused = await client.post("/admin/sources", headers={"x-csrf-token": csrf},
                                        json={"id": "xinghai", "script": LX_SOURCE.replace("'GET'", "'PUT'")})
            listing = await client.get("/admin/sources")
        return refused, listing, store

    refused, listing, store = asyncio.run(run())

    assert refused.status_code == 422 and "unsupported_method" in refused.json()["detail"]
    assert store.enabled() == () and listing.json()["items"] == []


def test_an_lx_source_is_stored_with_its_own_language_version_and_allowlist(tmp_path):
    async def run():
        client, store = _client(tmp_path)
        async with client:
            csrf = await _login(client)
            created = await client.post("/admin/sources", headers={"x-csrf-token": csrf},
                                        json={"id": "demo", "script": LX_SOURCE, "allow_any_host": True,
                                              "allow_insecure_http": True})
        return created, store

    created, store = asyncio.run(run())

    assert created.json()["plugin"] == {"sha256": hashlib.sha256(LX_SOURCE.encode()).hexdigest(),
                                        "version": "v3.2.11", "language": "javascript",
                                        "egress": {"allowed_hosts": ["music.example.com"],
                                                   "allowed_ports": [443], "allow_insecure_http": True,
                                                   "allow_ip_hosts": False, "allow_any_host": True}}
    stored = store.enabled()[0]
    assert stored.manifest.allowed_hosts == ("music.example.com",)


def test_deleting_a_source_removes_the_definition_and_the_script(tmp_path):
    async def run():
        client, store = _client(tmp_path)
        async with client:
            csrf = await _login(client)
            await client.post("/admin/sources", headers={"x-csrf-token": csrf}, json=_definition())
            digest = store.enabled()[0].manifest.sha256
            path = store.enabled()[0].path
            deleted = await client.delete("/admin/sources/demo", headers={"x-csrf-token": csrf})
            listing = await client.get("/admin/sources")
        return deleted, listing, store, digest, path

    deleted, listing, store, digest, path = asyncio.run(run())

    assert deleted.status_code == 200 and deleted.json()["uninstalled"] == [digest]
    assert listing.json()["items"] == [] and store.enabled() == ()
    assert not path.exists()


def test_deleting_an_unknown_source_is_not_found(tmp_path):
    async def run():
        client, _ = _client(tmp_path)
        async with client:
            csrf = await _login(client)
            return await client.delete("/admin/sources/missing", headers={"x-csrf-token": csrf})
    assert asyncio.run(run()).status_code == 404


def test_source_changes_are_bound_to_the_csrf_token(tmp_path):
    async def run():
        client, store = _client(tmp_path)
        async with client:
            csrf = await _login(client)
            await client.post("/admin/sources", headers={"x-csrf-token": csrf}, json=_definition())
            forged_create = await client.post("/admin/sources", headers={"x-csrf-token": "forged"}, json=_definition(id="other"))
            forged_delete = await client.delete("/admin/sources/demo", headers={"x-csrf-token": "forged"})
        return forged_create, forged_delete, store

    forged_create, forged_delete, store = asyncio.run(run())

    assert forged_create.status_code == 403 and forged_delete.status_code == 403
    assert [item.manifest.plugin_id for item in store.enabled()] == ["demo"]


def test_a_source_can_be_renamed_and_turned_off_without_touching_its_script(tmp_path):
    async def run():
        client, store = _client(tmp_path)
        async with client:
            csrf = await _login(client)
            await client.post("/admin/sources", headers={"x-csrf-token": csrf}, json=_definition())
            renamed = await client.patch("/admin/sources/demo", headers={"x-csrf-token": csrf},
                                         json={"name": "  星海  ", "enabled": False, "priority": 5})
            cleared = await client.patch("/admin/sources/demo", headers={"x-csrf-token": csrf}, json={"name": ""})
        return renamed, cleared, store

    renamed, cleared, store = asyncio.run(run())

    assert (renamed.json()["name"], renamed.json()["enabled"], renamed.json()["priority"]) == ("星海", False, 5)
    assert cleared.json()["name"] is None
    assert store.enabled()[0].manifest.plugin_id == "demo"


def test_the_audit_log_records_the_source_lifecycle_without_the_script(tmp_path):
    audit = EventLogStore()

    async def run():
        client, _ = _client(tmp_path, audit=audit)
        async with client:
            csrf = await _login(client)
            await client.post("/admin/sources", headers={"x-csrf-token": csrf}, json=_definition())
            await client.delete("/admin/sources/demo", headers={"x-csrf-token": csrf})
            return await client.get("/admin/audit")

    text = str(asyncio.run(run()).json())
    assert "create_source" in text and "delete_source" in text
    assert "def handle" not in text


def test_previewing_an_import_names_the_grants_and_stores_nothing(tmp_path):
    async def run():
        client, store = _client(tmp_path)
        async with client:
            csrf = await _login(client)
            preview = await client.post("/admin/sources/analyze", headers={"x-csrf-token": csrf},
                                        json={"id": "demo", "filename": "星海音乐源 v2.3.11.js",
                                              "script": LX_SOURCE})
            listing = await client.get("/admin/sources")
        return preview, listing, store

    preview, listing, store = asyncio.run(run())

    assert preview.status_code == 200
    body = preview.json()
    assert (body["install_path"], body["language"], body["installable"]) == ("lx", "javascript", False)
    assert body["operations"] == ["search", "resolve"]
    assert body["missing_grants"] == ["allow_any_host", "allow_insecure_http"]
    assert body["analysis"]["sha256"] == hashlib.sha256(LX_SOURCE.encode()).hexdigest()
    assert body["id"]["suggested"] == "demo-source"
    assert store.enabled() == () and listing.json()["items"] == []


def test_the_import_a_preview_asked_for_is_the_import_that_then_works(tmp_path):
    async def run():
        client, store = _client(tmp_path)
        async with client:
            csrf = await _login(client)
            preview = await client.post("/admin/sources/analyze", headers={"x-csrf-token": csrf},
                                        json={"id": "demo", "script": LX_SOURCE})
            created = await client.post("/admin/sources", headers={"x-csrf-token": csrf},
                                        json={"id": "demo", "script": LX_SOURCE,
                                              **preview.json()["required_grants"]})
        return preview, created, store

    preview, created, store = asyncio.run(run())

    assert preview.json()["missing_grants"] and preview.json()["installable"] is False
    assert created.status_code == 200
    assert created.json()["plugin"]["egress"] == {"allowed_hosts": ["music.example.com"],
                                                  "allowed_ports": [443], "allow_insecure_http": True,
                                                  "allow_ip_hosts": False, "allow_any_host": True}
    assert [item.manifest.plugin_id for item in store.enabled()] == ["demo"]


def test_a_preview_is_answered_without_the_plugin_volume(tmp_path):
    """A preview is a question about a script, not about the volume it lands on."""
    def unavailable():
        raise OSError("the volume is not mounted")

    async def run():
        app = FastAPI()
        auth = AdminAuth()
        auth.change_credentials("admin", "operator", "new-password")
        app.include_router(create_admin_router(auth=auth, plugins=unavailable))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
            csrf = await _login(client)
            return await client.post("/admin/sources/analyze", headers={"x-csrf-token": csrf},
                                     json={"id": "demo", "script": LX_SOURCE})

    response = asyncio.run(run())

    assert response.status_code == 200 and response.json()["installable"] is False


def test_a_preview_accepts_a_source_larger_than_one_plugin_payload(tmp_path):
    """The payload ceiling bounds a plugin step, not the import screen.

    A custom source is one file: the widest one an operator is asked to import
    is 144 KiB, which is past what a plugin may send the host in a single step.
    The import preview has to carry it anyway.
    """
    source = LX_SOURCE + "\n/* " + "padding " * 25000 + " */\n"
    assert len(source.encode()) > MAX_PAYLOAD_BYTES

    async def run():
        client, _ = _client(tmp_path)
        async with client:
            csrf = await _login(client)
            return await client.post("/admin/sources/analyze", headers={"x-csrf-token": csrf},
                                     json={"id": "demo", "script": source})

    response = asyncio.run(run())

    assert response.status_code == 200
    assert response.json()["analysis"]["sha256"] == hashlib.sha256(source.encode()).hexdigest()


def test_previewing_is_bound_to_the_csrf_token_and_refuses_an_empty_script(tmp_path):
    async def run():
        client, _ = _client(tmp_path)
        async with client:
            csrf = await _login(client)
            forged = await client.post("/admin/sources/analyze", json={"id": "demo", "script": LX_SOURCE})
            empty = await client.post("/admin/sources/analyze", headers={"x-csrf-token": csrf},
                                      json={"id": "demo", "script": "   "})
            return forged, empty

    forged, empty = asyncio.run(run())

    assert forged.status_code == 403
    assert empty.status_code == 422 and "script" in empty.json()["detail"]
