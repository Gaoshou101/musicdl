import pytest
import json
from pathlib import Path
from uuid import UUID
from pydantic import ValidationError

from musicdl.contracts.plugin import (
    ArtifactResult,
    PluginRequest,
    PluginResponse,
)


def test_plugin_contract_accepts_v1_operations_and_fixture_shape():
    fixture_dir = Path(__file__).parents[1] / "fixtures" / "contracts"
    request = PluginRequest.model_validate(json.loads((fixture_dir / "plugin-search-request.json").read_text()))
    response = PluginResponse.model_validate(json.loads((fixture_dir / "plugin-search-response.json").read_text()))
    assert request.protocol == "musicdl.plugin/v1"
    assert isinstance(request.request_id, UUID)
    assert response.protocol == request.protocol
    assert response.request_id == request.request_id
    assert response.operation == request.operation
    assert response.result == []


def test_plugin_contract_rejects_wrong_protocol_and_extra_fields():
    with pytest.raises(ValidationError):
        PluginRequest(
            protocol="musicdl.plugin/v2",
            request_id="123e4567-e89b-12d3-a456-426614174000",
            operation="health",
        )
    with pytest.raises(ValidationError):
        PluginRequest(
            protocol="musicdl.plugin/v1",
            request_id="123e4567-e89b-12d3-a456-426614174000",
            operation="health",
            unexpected=True,
        )


def test_plugin_request_requires_valid_uuid_request_id_and_json_payload():
    with pytest.raises(ValidationError):
        PluginRequest(protocol="musicdl.plugin/v1", request_id="not-a-uuid", operation="health")
    with pytest.raises(ValidationError):
        PluginRequest(
            protocol="musicdl.plugin/v1",
            request_id="123e4567-e89b-12d3-a456-426614174000",
            operation="health",
            payload={"not_json": {1, 2}},
        )


def test_plugin_contract_bounds_timeout_and_payload_size():
    with pytest.raises(ValidationError):
        PluginRequest(protocol="musicdl.plugin/v1", request_id="123e4567-e89b-12d3-a456-426614174000", operation="health", timeout_ms=0)
    with pytest.raises(ValidationError):
        PluginRequest(protocol="musicdl.plugin/v1", request_id="123e4567-e89b-12d3-a456-426614174000", operation="health", timeout_ms=31_000)
    with pytest.raises(ValidationError):
        PluginRequest(protocol="musicdl.plugin/v1", request_id="123e4567-e89b-12d3-a456-426614174000", operation="health", payload={"x": "a" * 70_000})


def test_plugin_artifact_result_rejects_absolute_and_traversal_paths():
    for path in ("../secret.mp3", "..\\secret.mp3", "/tmp/out.mp3", "C:\\temp\\out.mp3", "artifacts//song.mp3"):
        with pytest.raises(ValidationError):
            ArtifactResult(relative_path=path, sha256="a" * 64, size_bytes=1)


def test_plugin_artifact_result_accepts_double_dot_filename():
    artifact = ArtifactResult(relative_path="artifacts/song..mp3", sha256="a" * 64, size_bytes=1)
    assert artifact.relative_path == "artifacts/song..mp3"


def test_plugin_response_requires_consistent_success_or_structured_error():
    request_id = "123e4567-e89b-12d3-a456-426614174000"
    assert PluginResponse(protocol="musicdl.plugin/v1", request_id=request_id, operation="health", ok=True, result={}).ok
    assert PluginResponse(protocol="musicdl.plugin/v1", request_id=request_id, operation="health", ok=False, error={"code": "failed", "message": "nope"}).error.code == "failed"
    with pytest.raises(ValidationError):
        PluginResponse(protocol="musicdl.plugin/v1", request_id=request_id, operation="health", ok=True)
    with pytest.raises(ValidationError):
        PluginResponse(protocol="musicdl.plugin/v1", request_id=request_id, operation="health", ok=False)
    with pytest.raises(ValidationError):
        PluginResponse(protocol="musicdl.plugin/v1", request_id=request_id, operation="health", ok=True, result={}, error={"code": "x", "message": "bad"})
