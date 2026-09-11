from fastapi import FastAPI

from .config import AppSettings

app = FastAPI(title="musicdl", docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz() -> dict[str, object]:
    settings = AppSettings()
    return {"service": "musicdl", "status": "ok", "config_version": settings.config.version}
