from fastapi import FastAPI

from musicdl.contracts.plugin import PROTOCOL

app = FastAPI(title="musicdl-plugin-runner", docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"service": "plugin-runner", "status": "ok", "protocol": PROTOCOL}
