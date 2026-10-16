"""Disposable FastAPI fixture for the real exported-panel browser smoke.

Only the source URL fetch boundary is fixed in memory. Login, cookies, CSRF,
source-list normalization, source analysis and installation, search, downloads,
and history all run through the application's real routes and stores.
"""

from __future__ import annotations

import json
import os
import asyncio
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from musicdl.admin.auth import PasswordHasher
from musicdl.admin.store import AdminStateStore
from musicdl.admin import portal
from musicdl.app import create_app
from musicdl.config import AppSettings
from musicdl.media.models import DownloadMetadata
from musicdl.sources import SourceEntry, SourceRegistry
from musicdl.sources.models import Candidate


CATALOG_URL = "https://catalog.example/browser/catalog.json"
SCRIPT_URL = "https://sources.example/browser-smoke.js"
CATALOG = json.dumps({
    "format": "musicdl-source-list/v1",
    "sources": [{"url": SCRIPT_URL}],
})
SOURCE_SCRIPT = "export default { name: 'Browser Smoke Source', version: '1.0.0' };\n"
AUDIO = b"ID3\x04\x00\x00\x00\x00\x00\x00frame-payload"


class BrowserSmokeSource:
    def __init__(self) -> None:
        self.resolve_started = asyncio.Event()
        self.release_resolve = asyncio.Event()
        self.transfer_started = asyncio.Event()
        self.release_transfer = asyncio.Event()

    async def search(self, _query: str):
        return [Candidate(
            source_id="browser-fixture",
            source_version="1.0.0",
            item_id="browser-smoke-track",
            title="Browser Smoke Track",
            artist="musicdl CI",
            format="mp3",
            size=len(AUDIO),
        )]

    async def download(self, _candidate: Candidate, **_kwargs) -> DownloadMetadata:
        self.resolve_started.set()
        await self.release_resolve.wait()

        async def chunks():
            self.transfer_started.set()
            await self.release_transfer.wait()
            yield AUDIO

        return DownloadMetadata(
            chunks=chunks(),
            extension="mp3",
            media_type="audio/mpeg",
            declared_size=len(AUDIO),
        )

    async def health(self) -> bool:
        return True


class BrowserSmokeRuntime:
    def __init__(self, source: BrowserSmokeSource) -> None:
        self.registry = SourceRegistry([SourceEntry("browser-fixture", "1.0.0", source)])
        self.resolvers = {"browser-fixture": source}
        self.plugin_registry = None
        self.state = None
        self.service = None
        self.message_worker = None
        self.job_worker = None

    async def aclose(self) -> None:
        return None


def _fixture_fetch(url: str, **_kwargs):
    """Return fixed bytes only for the two HTTPS fixtures used by this smoke."""
    if url == CATALOG_URL:
        return ({"script": CATALOG, "filename": "catalog.json", "language": "javascript"},
                "application/json")
    if url == SCRIPT_URL:
        return ({"script": SOURCE_SCRIPT, "filename": "browser-smoke.js", "language": "javascript"},
                "text/javascript")
    from musicdl.admin.source_fetch import SourceFetchError

    raise SourceFetchError("url_denied")


def create_fixture_app(root: Path, panel_root: Path) -> FastAPI:
    root.mkdir(parents=True, exist_ok=True)
    media_root = root / "media"
    plugin_root = root / "plugins"
    telegram_root = root / "telegram"
    state_path = root / "admin-state.json"
    for path in (media_root, plugin_root, telegram_root):
        path.mkdir(parents=True, exist_ok=True)

    AdminStateStore(state_path).save(
        credentials={
            "user_id": "browser-smoke",
            "password_hash": PasswordHasher().hash("browser-smoke-password"),
            "must_change": False,
            "credential_version": 1,
        },
        sources=[],
        bots=[],
        settings={},
    )

    portal.fetch_source_with_metadata = _fixture_fetch
    source = BrowserSmokeSource()
    settings = AppSettings(
        admin={"cookie_secure": False, "panel_root": str(panel_root)},
        media={"verify_duration": "lenient"},
        worker={
            "quality_policy": "best_available",
            "search_timeout": 1.0,
            "resolve_stream_timeout": 26.0,
            "health_timeout": 1.0,
            "job_timeout": 30.0,
            "budget_slack_seconds": 2.0,
        },
    )
    # Deployment paths are validated as POSIX container mounts. This disposable
    # fixture also runs on Windows, so supply native temporary paths after that
    # validation, just as the unit-test fixtures do.
    settings.admin.state_path = str(state_path)
    settings.media.root = str(media_root)
    settings.plugin.app_data_root = str(plugin_root)
    settings.telegram.session_root = str(telegram_root)
    app = create_app(
        settings,
        runtime_factory=lambda _settings: BrowserSmokeRuntime(source),
    )

    @app.middleware("http")
    async def fixture_controls(request, call_next):
        # Loopback-only controls hold real resolve and transfer spans open long
        # enough for a second browser tab to read each running history state.
        if request.url.path == "/__browser_fixture__/wait-for-resolve" and request.method == "GET":
            try:
                await asyncio.wait_for(source.resolve_started.wait(), timeout=30)
            except TimeoutError:
                return JSONResponse({"detail": "fixture resolve did not start"}, status_code=504)
            return JSONResponse({"started": True})
        if request.url.path == "/__browser_fixture__/release-resolve" and request.method == "POST":
            source.release_resolve.set()
            return JSONResponse({"released": True})
        if request.url.path == "/__browser_fixture__/wait-for-transfer" and request.method == "GET":
            try:
                await asyncio.wait_for(source.transfer_started.wait(), timeout=30)
            except TimeoutError:
                return JSONResponse({"detail": "fixture transfer did not start"}, status_code=504)
            return JSONResponse({"started": True})
        if request.url.path == "/__browser_fixture__/release-transfer" and request.method == "POST":
            source.release_transfer.set()
            return JSONResponse({"released": True})
        return await call_next(request)

    return app


def main() -> None:
    panel_root_value = os.environ.get("MUSICDL_BROWSER_PANEL_ROOT")
    if not panel_root_value:
        raise SystemExit("MUSICDL_BROWSER_PANEL_ROOT must point to the built static panel")
    panel_root = Path(panel_root_value).resolve()
    if not (panel_root / "index.html").is_file():
        raise SystemExit(f"built panel index missing under {panel_root}")
    port = int(os.environ.get("MUSICDL_BROWSER_PORT", "3187"))
    with tempfile.TemporaryDirectory(prefix="musicdl-browser-smoke-") as directory:
        app = create_fixture_app(Path(directory), panel_root)
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
