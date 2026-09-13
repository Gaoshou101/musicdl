from contextlib import asynccontextmanager
from typing import Any, Callable
import time

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response

from .config import AppSettings
from .wecom.service import WeComService
from .wecom.state import RedisStateStore, StateUnavailable


def create_app(settings: AppSettings | None = None, state_factory: Callable[[Any], Any] | None = None, clock=None) -> FastAPI:
    settings = settings or AppSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = None
        redis = None
        if settings.wecom.enabled:
            if state_factory:
                state = state_factory(settings)
            else:
                from redis.asyncio import Redis
                redis = Redis.from_url(
                    settings.redis.url.get_secret_value(),
                    decode_responses=False,
                    socket_connect_timeout=settings.redis.connect_timeout,
                    socket_timeout=settings.redis.operation_timeout,
                )
                state = RedisStateStore(redis)
            app.state.wecom_state = state
            app.state.wecom_service = WeComService(settings.wecom, state, clock or time.time)
        try:
            yield
        finally:
            if redis is not None:
                await redis.aclose()

    app = FastAPI(title="musicdl", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"service": "musicdl", "status": "ok", "config_version": settings.config.version}

    @app.get("/readyz")
    async def readyz() -> Response:
        if not settings.wecom.enabled:
            return PlainTextResponse("ready")
        try:
            if not await app.state.wecom_state.ping():
                return PlainTextResponse("not ready", status_code=503)
        except (AttributeError, StateUnavailable):
            return PlainTextResponse("not ready", status_code=503)
        return PlainTextResponse("ready")

    @app.get("/wecom/callback", response_class=PlainTextResponse)
    async def wecom_get(request: Request) -> PlainTextResponse:
        if not settings.wecom.enabled:
            return PlainTextResponse("disabled", status_code=503)
        try:
            value = await app.state.wecom_service.verify_get(request)
        except ValueError:
            return PlainTextResponse("bad request", status_code=400)
        return PlainTextResponse(value)

    @app.post("/wecom/callback")
    async def wecom_post(request: Request) -> Response:
        if not settings.wecom.enabled:
            return PlainTextResponse("disabled", status_code=503)
        try:
            await app.state.wecom_service.handle_post(request)
        except ValueError as exc:
            if str(exc) == "unsupported_encoding":
                return PlainTextResponse("unsupported media", status_code=415)
            if str(exc) == "body_too_large":
                return PlainTextResponse("payload too large", status_code=413)
            return PlainTextResponse("bad request", status_code=400)
        except PermissionError:
            return PlainTextResponse("forbidden", status_code=403)
        except StateUnavailable:
            return PlainTextResponse("not ready", status_code=503)
        return Response(status_code=200)

    return app


app = create_app()
