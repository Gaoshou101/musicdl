from musicdl.admin.management import SourceManager
from fastapi import FastAPI
import asyncio
import httpx
from musicdl.admin.portal import create_admin_router
from musicdl.admin.auth import AdminAuth
from musicdl.admin.health import EventLogStore


def test_source_management_updates_enabled_priority_and_timeout():
    manager = SourceManager([{"id": "netease", "enabled": True, "priority": 2, "timeout": 10.0}])
    updated = manager.update("netease", enabled=False, priority=1, timeout=3.5)
    assert updated == {"id": "netease", "enabled": False, "priority": 1, "timeout": 3.5, "name": None}
    assert manager.list() == [updated]


def test_a_source_name_is_a_bounded_label_that_can_be_cleared():
    manager = SourceManager([{"id": "xinghai"}])
    assert manager.update("xinghai", name="  星海音乐源  ")["name"] == "星海音乐源"
    # An empty label clears it; a null one keeps whatever was stored.
    assert manager.update("xinghai", name=None)["name"] == "星海音乐源"
    assert manager.update("xinghai", name="")["name"] is None
    for value in ("x" * 121, "line\nbreak", 7):
        try:
            manager.update("xinghai", name=value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid name accepted: {value!r}")


def test_unknown_source_is_rejected():
    manager = SourceManager()
    try:
        manager.update("missing", enabled=True)
    except KeyError:
        pass
    else:
        raise AssertionError("unknown source accepted")


def test_source_api_exposes_and_updates_configuration():
    app = FastAPI()
    auth = AdminAuth(); auth.change_credentials("admin", "operator", "new-password")
    app.include_router(create_admin_router(auth=auth, sources=SourceManager([{"id": "x"}])))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
            login = await client.post("/admin/login", json={"username": "operator", "password": "new-password"})
            csrf = login.json()["csrf_token"]
            return await client.get("/admin/sources"), await client.patch("/admin/sources/x", headers={"x-csrf-token": csrf}, json={"enabled": False, "priority": 4, "timeout": 2})
    listing, updated = asyncio.run(run())
    assert listing.json()["items"][0]["id"] == "x"
    result = updated.json()
    assert result["enabled"] is False and result["priority"] == 4 and result["timeout"] == 2.0


def test_dashboard_must_change_warning_and_strict_manager_inputs():
    auth = AdminAuth(); app = FastAPI(); app.include_router(create_admin_router(auth=auth, sources=SourceManager([{"id": "<source>"}])))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as c:
            login = await c.post("/admin/login", json={"username":"admin","password":"password"})
            page = await c.get("/admin/", cookies=None)
            return login, page
    login, page = asyncio.run(run())
    assert login.status_code == 200 and "credential change required" in page.text and "&lt;source&gt;" in page.text


def test_manager_rejects_coercible_values_and_bot_type_exists():
    from musicdl.admin.management import BotManager
    for item in ({"id":"x", "enabled":"false"}, {"id":"x", "priority":"1"}, {"id":"x", "timeout":"2"}):
        try: SourceManager([item])
        except ValueError: pass
        else: raise AssertionError("coercible config accepted")
    assert isinstance(BotManager([{"id":"b", "username":"MusicBot"}]), SourceManager)
    # A bot definition without an addressable username is not a definition.
    for item in ({"id":"b"}, {"id":"b", "username":""}, {"id":"b", "username":"@MusicBot"},
                 {"id":"b", "username":"MusicBot", "command_template":"no placeholder"},
                 {"id":"b", "username":"MusicBot", "command_template":"{query} and {query}"},
                 {"id":"b", "username":"MusicBot", "timeout":121}):
        try: BotManager([item])
        except ValueError: pass
        else: raise AssertionError(f"invalid bot definition accepted: {item}")


def test_http_change_credentials_revokes_old_session_and_requires_change():
    auth = AdminAuth(); app = FastAPI(); app.include_router(create_admin_router(auth=auth, sources=SourceManager([{"id":"s"}])))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as c:
            login = await c.post("/admin/login", json={"username":"admin","password":"password"}); old = c.cookies.get("admin_session"); csrf = login.json()["csrf_token"]
            blocked = await c.get("/admin/sources")
            changed = await c.post("/admin/change-credentials", headers={"x-csrf-token":csrf}, json={"password":"password","username":"operator","new_password":"new-password"})
            c.cookies.set("admin_session", old); old_access = await c.get("/admin/sources")
            old_login = await c.post("/admin/login", json={"username":"admin","password":"password"})
            new_login = await c.post("/admin/login", json={"username":"operator","password":"new-password"})
            return blocked, changed, old_access, old_login, new_login
    blocked, changed, old_access, old_login, new_login = asyncio.run(run())
    assert blocked.status_code == 403 and changed.status_code == 200 and old_access.status_code == 401 and old_login.status_code == 401 and new_login.status_code == 200


def test_http_bound_csrf_updates_source_and_bot_and_audits_actions():
    from musicdl.admin.management import BotManager
    auth=AdminAuth(); audit=EventLogStore(); app=FastAPI(); app.include_router(create_admin_router(auth=auth, audit=audit, sources=SourceManager([{"id":"s"}]), bots=BotManager([{"id":"b", "username":"MusicBot"}])))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as c:
            l=await c.post("/admin/login",json={"username":"admin","password":"password"}); csrf=l.json()["csrf_token"]
            await c.post("/admin/change-credentials",headers={"x-csrf-token":csrf},json={"password":"password","username":"operator","new_password":"new-password"}); csrf=c.cookies.get("csrf_token")
            fake=await c.patch("/admin/sources/s",headers={"x-csrf-token":"fake"},json={"enabled":False}); src=await c.patch("/admin/sources/s",headers={"x-csrf-token":csrf},json={"enabled":False}); bot=await c.patch("/admin/bots/b",headers={"x-csrf-token":csrf},json={"enabled":False}); aud=await c.get("/admin/audit")
            return fake,src,bot,aud
    fake,src,bot,aud=asyncio.run(run()); text=str(aud.json()); assert fake.status_code==403 and src.status_code==200 and bot.status_code==200 and all(x in text for x in ("login","change_credentials","update_source","update_bot")) and all(x not in text for x in ("new-password","csrf_token","admin_session"))


def test_http_rate_limit_and_audit_pagination_validation():
    auth=AdminAuth(); audit=EventLogStore(); app=FastAPI(); app.include_router(create_admin_router(auth=auth,audit=audit,limiter=__import__('musicdl.admin.auth',fromlist=['RateLimiter']).RateLimiter(limit=1)))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as c:
            await c.post("/admin/login",json={"username":"bad","password":"bad"}); limited=await c.post("/admin/login",json={"username":"bad","password":"bad"}); return limited,[await c.get(f"/admin/audit?offset={x}") for x in (-1,0,201)]
    limited,pages=asyncio.run(run()); assert limited.status_code==429 and all(p.status_code in (401,422) for p in pages)
