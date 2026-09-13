import hashlib
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from musicdl_plugin_runner.app import app


def body(source="ok"):
    return {"manifest": {"plugin_id": "p", "version": "1", "language": "python", "operations": ["search"], "sha256": hashlib.sha256(source.encode()).hexdigest()}, "source": source, "request": {"protocol": "musicdl.plugin/v1", "request_id": str(uuid4()), "operation": "search"}}


def test_healthz():
    assert TestClient(app).get("/healthz").status_code == 200


def test_invalid_request_is_bounded_error():
    response = TestClient(app).post("/v1/execute", content=json.dumps({"source": "x"}))
    assert response.status_code in (400, 422, 500)
    assert "source" not in response.text or response.status_code == 422


def test_oversized_stream_rejected():
    source = "x" * (6 * 1024 * 1024)
    response = TestClient(app).post("/v1/execute", content=json.dumps(body(source)))
    assert response.status_code == 413
