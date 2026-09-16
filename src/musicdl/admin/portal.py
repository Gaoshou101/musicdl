from __future__ import annotations

import secrets
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from html import escape

from .auth import AdminAuth, RateLimiter
from .health import EventLogStore, HealthAggregator
from .management import BotManager, SourceManager


def create_admin_router(*, auth: AdminAuth | None = None, sources: SourceManager | None = None,
                        health: HealthAggregator | None = None, events: EventLogStore | None = None,
                        limiter: RateLimiter | None = None, bots: SourceManager | None = None,
                        audit: EventLogStore | None = None) -> APIRouter:
    auth, sources, health, events, limiter = auth or AdminAuth(), sources or SourceManager(), health or HealthAggregator({}), events or EventLogStore(), limiter or RateLimiter()
    bots, audit = bots or BotManager(), audit or EventLogStore()
    router = APIRouter(prefix="/admin")

    def require(request: Request):
        session = request.cookies.get("admin_session")
        if not auth.session_user(session): raise HTTPException(401, "authentication required")
        if auth.session_must_change(session) and request.url.path not in {"/admin/", "/admin/change-credentials"}: raise HTTPException(403, "credential change required")
        return session

    def mutate(request: Request):
        session = require(request)
        if not auth.valid_csrf(session, request.headers.get("x-csrf-token")): raise HTTPException(403, "CSRF validation failed")
        return session

    @router.post("/login")
    async def login(request: Request, response: Response):
        if not limiter.allow(request.client.host if request.client else "unknown"):
            audit.append({"action": "login", "status": "rate_limited"})
            raise HTTPException(429, "too many login attempts")
        body = await request.json()
        result = auth.authenticate(str(body.get("username", "")), str(body.get("password", "")))
        if not result.ok:
            audit.append({"action": "login", "status": "failed"})
            raise HTTPException(401, "invalid credentials")
        session = auth.issue_session()
        csrf = secrets.token_urlsafe(24)
        auth.bind_csrf(session, csrf)
        response.set_cookie("admin_session", session, httponly=True, secure=True, samesite="lax")
        response.set_cookie("csrf_token", csrf, httponly=False, secure=True, samesite="lax")
        audit.append({"action": "login", "status": "success"})
        return {"ok": True, "must_change": result.must_change, "csrf_token": csrf}

    @router.post("/change-credentials")
    async def change_credentials(request: Request, response: Response):
        session = mutate(request); body = await request.json()
        result = auth.authenticate(auth.session_user(session) or "", str(body.get("password", "")))
        if not result.ok: raise HTTPException(401, "invalid credentials")
        try:
            auth.change_credentials(result.user_id or "", str(body.get("username", "")), str(body.get("new_password", "")))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        replacement = auth.issue_session(); csrf = secrets.token_urlsafe(24); auth.bind_csrf(replacement, csrf)
        response.set_cookie("admin_session", replacement, httponly=True, secure=True, samesite="lax")
        response.set_cookie("csrf_token", csrf, secure=True, samesite="lax")
        audit.append({"action": "change_credentials", "status": "success"})
        return {"ok": True, "csrf_token": csrf}

    @router.get("/sources")
    async def list_sources(request: Request):
        require(request)
        return {"items": sources.list()}

    @router.patch("/sources/{source_id}")
    async def update_source(source_id: str, body: dict, request: Request):
        mutate(request)
        try:
            result = sources.update(source_id, enabled=body.get("enabled"), priority=body.get("priority"), timeout=body.get("timeout")); audit.append({"action": "update_source", "source_id": source_id, "status": "success"}); return result
        except KeyError:
            raise HTTPException(404, "source not found") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/bots")
    async def list_bots(request: Request):
        require(request); return {"items": bots.list()}

    @router.post("/bots")
    async def create_bot(body: dict, request: Request):
        mutate(request)
        try:
            created = bots.register(body, persist=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        audit.append({"action": "create_bot", "bot_id": created["id"], "status": "success"})
        return created

    @router.patch("/bots/{bot_id}")
    async def update_bot(bot_id: str, body: dict, request: Request):
        mutate(request)
        try: result = bots.update(bot_id, enabled=body.get("enabled"), priority=body.get("priority"), timeout=body.get("timeout"), username=body.get("username"), command_template=body.get("command_template"))
        except KeyError: raise HTTPException(404, "bot not found") from None
        except ValueError as exc: raise HTTPException(422, str(exc)) from None
        audit.append({"action": "update_bot", "bot_id": bot_id, "status": "success"}); return result

    @router.delete("/bots/{bot_id}")
    async def delete_bot(bot_id: str, request: Request):
        mutate(request)
        try:
            removed = bots.remove(bot_id)
        except KeyError:
            raise HTTPException(404, "bot not found") from None
        audit.append({"action": "delete_bot", "bot_id": bot_id, "status": "success"})
        return removed

    @router.get("/health")
    async def health_report(request: Request):
        require(request)
        return await health.check()

    @router.get("/events")
    async def event_page(request: Request, offset: int = 0, limit: int = 50):
        require(request)
        if offset < 0 or limit < 1 or limit > 200:
            raise HTTPException(422, "invalid pagination")
        return events.page(offset=offset, limit=limit)

    @router.get("/audit")
    async def audit_page(request: Request, offset: int = 0, limit: int = 50):
        require(request)
        if offset < 0 or limit < 1 or limit > 200: raise HTTPException(422, "invalid pagination")
        return audit.page(offset=offset, limit=limit)

    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        require(request)
        warning = "<p>SECURITY WARNING: credential change required (强制修改)。</p>" if auth.session_must_change(request.cookies.get("admin_session")) else ""
        source_ids = ''.join(f"<li>{escape(item['id'])}</li>" for item in sources.list())
        bot_ids = ''.join(
            f"<li>{escape(item['id'])}{'' if item.get('username') is None else ' (' + escape(item['username']) + ')'}</li>"
            for item in bots.list())
        report = await health.check()
        health_html = " ".join(f"<span>{escape(str(k))}: {escape(str(v))}</span>" for k,v in report["checks"].items())
        event_page = events.page(offset=0, limit=1); audit_page = audit.page(offset=0, limit=1)
        recent = escape(str(event_page["items"][0])) if event_page["items"] else "none"
        audit_recent = escape(str(audit_page["items"][0])) if audit_page["items"] else "none"
        return HTMLResponse(render_dashboard(warning=warning, sources=source_ids, bots=bot_ids, health=health_html, event_total=event_page["total"], recent=recent, audit_total=audit_page["total"], audit_recent=audit_recent))

    return router


def render_dashboard(*, template_path: str | Path | None = None, **values: object) -> str:
    """Render the package template, with a safe built-in fallback."""
    try:
        path = Path(template_path) if template_path is not None else Path(__file__).parent.parent / "templates" / "admin_dashboard.html"
        template = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        template = "<html><body><h1>musicdl 管理后台</h1>{{warning}}<div>{{health}}</div><ul>{{sources}}</ul><ul>{{bots}}</ul><div>events={{event_total}} {{recent}}</div><div>audit={{audit_total}} {{audit_recent}}</div></body></html>"
    return template.replace("{{warning}}", str(values.get("warning", ""))).replace("{{sources}}", str(values.get("sources", ""))).replace("{{bots}}", str(values.get("bots", ""))).replace("{{health}}", str(values.get("health", ""))).replace("{{event_total}}", str(values.get("event_total", 0))).replace("{{recent}}", str(values.get("recent", "none"))).replace("{{audit_total}}", str(values.get("audit_total", 0))).replace("{{audit_recent}}", str(values.get("audit_recent", "none")))
