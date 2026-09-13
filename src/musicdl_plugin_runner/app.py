from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from musicdl.contracts.plugin import MAX_INVOCATION_BYTES, PROTOCOL, PluginInvocation
from musicdl_plugin_runner.supervisor import Supervisor, build_host_command

app = FastAPI(title="musicdl-plugin-runner", docs_url=None, redoc_url=None)
def _unavailable_builder(_invocation: PluginInvocation) -> list[str]:
    raise RuntimeError("host builders are configured by the runtime")


supervisor = Supervisor(build_host_command)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"service": "plugin-runner", "status": "ok", "protocol": PROTOCOL}


@app.post("/v1/execute")
async def execute(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_INVOCATION_BYTES:
            return JSONResponse({"error": {"code": "invocation_too_large", "message": "invocation exceeds limit"}}, status_code=413)
    try:
        invocation = PluginInvocation.model_validate_json(bytes(body))
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": {"code": "invalid_invocation", "message": "invalid invocation"}}, status_code=400)
    step = await supervisor.execute(invocation)
    if step.response and step.response.error and step.response.error.code == "busy":
        status = 429
    elif step.response and step.response.error:
        status = {"invalid_invocation": 400, "invocation_too_large": 413, "timeout": 504, "busy": 429}.get(step.response.error.code, 502)
    else:
        status = 200
    return JSONResponse(step.model_dump(mode="json"), status_code=status)
