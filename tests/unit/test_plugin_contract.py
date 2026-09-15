import pytest
import json
import base64
import hashlib
from pathlib import Path
from uuid import UUID
from pydantic import ValidationError

from musicdl.contracts.plugin import (
    ArtifactResult,
    PluginRequest,
    PluginResponse,
    HttpAction,
    HttpObservation,
    PluginInvocation,
    PluginManifest,
    PluginStep,
    PROTOCOL,
)


def _resolved_media(**overrides):
    values = {
        "candidate_id": "candidate-1",
        "url": "https://media.example.test/path/song.mp3?token=opaque",
        "extension": "mp3",
        "media_type": "audio/mpeg",
        "declared_size": 123,
    }
    values.update(overrides)
    from musicdl.contracts.plugin import ResolvedMedia

    return ResolvedMedia(**values)


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


def _manifest(source="def handle(request): return request", **overrides):
    values = dict(
        plugin_id="demo.source", version="1.0.0", language="python", operations=("search",),
        allowed_hosts=("api.example.com",), sha256=hashlib.sha256(source.encode()).hexdigest(),
    )
    values.update(overrides)
    return PluginManifest(**values)


def _invocation(**overrides):
    source = overrides.pop("source", "def handle(request): return request")
    return PluginInvocation(
        manifest=overrides.pop("manifest", _manifest(source)), source=source,
        request=overrides.pop("request", PluginRequest(protocol=PROTOCOL, request_id="123e4567-e89b-12d3-a456-426614174000", operation="search")),
        **overrides,
    )


def test_restricted_plugin_invocation_contract_accepts_valid_shape():
    assert _invocation().manifest.plugin_id == "demo.source"


def test_restricted_plugin_manifest_rejects_unsafe_ids_hosts_and_duplicates():
    with pytest.raises(ValidationError):
        _manifest(plugin_id="../unsafe")
    with pytest.raises(ValidationError):
        _manifest(allowed_hosts=("api.example.com", "api.example.com"))
    with pytest.raises(ValidationError):
        _manifest(operations=("search", "search"))
    for host in ("127.0.0.1", "*.example.com", "https://example.com", "example.com:443", "example.com/path", "API.example.com"):
        with pytest.raises(ValidationError):
            _manifest(allowed_hosts=(host,))


def test_restricted_plugin_invocation_rejects_source_digest_operation_and_actions():
    with pytest.raises(ValidationError):
        _invocation(source="bad\x00source")
    with pytest.raises(ValidationError):
        _invocation(source="x" * (128 * 1024 + 1))
    with pytest.raises(ValidationError):
        _invocation(manifest=_manifest(sha256="b" * 64))
    with pytest.raises(ValidationError):
        _invocation(request=PluginRequest(protocol=PROTOCOL, request_id="123e4567-e89b-12d3-a456-426614174000", operation="download"))
    action = HttpAction(action_id="a1", method="GET", url="https://api.example.com/search")
    observation = HttpObservation(action_id="a1", status_code=200, body=base64.b64encode(b"ok").decode())
    assert _invocation(actions=(action,), observations=(observation,)).actions[0] == action
    with pytest.raises(ValidationError):
        HttpAction(action_id="a1", method="POST", url="https://api.example.com/search")
    with pytest.raises(ValidationError):
        HttpObservation(action_id="a1", status_code=200, body=base64.b64encode(b"x" * (1024 * 1024 + 1)).decode())
    actions = tuple(HttpAction(action_id=f"a{i}", method="GET", url="https://api.example.com/search") for i in range(5))
    with pytest.raises(ValidationError):
        _invocation(actions=actions)
    with pytest.raises(ValidationError):
        _invocation(actions=(action,), observations=(HttpObservation(action_id="missing", status_code=200),))
    four_actions = tuple(HttpAction(action_id=f"b{i}", method="GET", url="https://api.example.com/search") for i in range(4))
    five_observations = tuple(HttpObservation(action_id="b0", status_code=200) for _ in range(5))
    with pytest.raises(ValidationError):
        _invocation(actions=four_actions, observations=five_observations)


def test_plugin_step_requires_exactly_one_response_or_action():
    action = HttpAction(action_id="a1", method="GET", url="https://api.example.com/search")
    response = PluginResponse(protocol=PROTOCOL, request_id="123e4567-e89b-12d3-a456-426614174000", operation="search", ok=True, result={})
    assert PluginStep(action=action).action == action
    assert PluginStep(response=response).response == response
    with pytest.raises(ValidationError):
        PluginStep()
    with pytest.raises(ValidationError):
        PluginStep(action=action, response=response)


@pytest.mark.parametrize(
    ("extension", "media_type"),
    (
        ("mp3", "audio/mpeg"),
        ("flac", "audio/flac"),
        ("m4a", "audio/mp4"),
        ("m4a", "audio/x-m4a"),
        ("ogg", "audio/ogg"),
        ("ogg", "application/ogg"),
    ),
)
def test_resolved_media_accepts_supported_descriptor_formats(extension, media_type):
    media = _resolved_media(extension=extension, media_type=media_type)

    assert media.candidate_id == "candidate-1"
    assert media.extension == extension
    assert media.media_type == media_type
    assert media.model_dump() == {
        "candidate_id": "candidate-1",
        "url": "https://media.example.test/path/song.mp3?token=opaque",
        "extension": extension,
        "media_type": media_type,
        "declared_size": 123,
    }


def test_resolved_media_accepts_size_boundaries_and_optional_size():
    from musicdl.contracts.plugin import RESOLVED_MEDIA_MAX_BYTES

    assert _resolved_media(declared_size=0).declared_size == 0
    assert _resolved_media(declared_size=RESOLVED_MEDIA_MAX_BYTES).declared_size == RESOLVED_MEDIA_MAX_BYTES
    assert _resolved_media(declared_size=None).declared_size is None


@pytest.mark.parametrize("field", ("headers", "data", "bytes"))
def test_resolved_media_rejects_unknown_fields(field):
    with pytest.raises(ValidationError):
        _resolved_media(**{field: {}})


@pytest.mark.parametrize(
    "url",
    (
        "http://media.example.test/song.mp3",
        "ftp://media.example.test/song.mp3",
        "//media.example.test/song.mp3",
        "https://user:password@media.example.test/song.mp3",
        "https://user@media.example.test/song.mp3",
        "https://media.example.test/song.mp3#fragment",
        "https://media.example.test:443/song.mp3",
        "https://media.example.test:8443/song.mp3",
        "https://media.example.test/song.mp3\n",
        "https://media.example.test/song.mp3\t",
        "https://media.example.test/song.mp3\x00",
        "https://media.example.test/song.mp3\x7f",
    ),
)
def test_resolved_media_rejects_unsafe_urls(url):
    with pytest.raises(ValidationError):
        _resolved_media(url=url)


@pytest.mark.parametrize(
    ("extension", "media_type"),
    (
        ("mp3", "audio/flac"),
        ("flac", "audio/mpeg"),
        ("m4a", "audio/ogg"),
        ("ogg", "audio/mp4"),
    ),
)
def test_resolved_media_rejects_mismatched_extension_and_media_type(extension, media_type):
    with pytest.raises(ValidationError):
        _resolved_media(extension=extension, media_type=media_type)


@pytest.mark.parametrize("declared_size", (-1, 500 * 1024 * 1024 + 1, True, 1.5, "1"))
def test_resolved_media_rejects_invalid_declared_sizes(declared_size):
    with pytest.raises(ValidationError):
        _resolved_media(declared_size=declared_size)


def test_resolved_media_is_frozen_and_uses_only_the_five_contract_fields():
    media = _resolved_media()

    with pytest.raises(ValidationError):
        media.extension = "flac"
    assert set(media.model_dump()) == {"candidate_id", "url", "extension", "media_type", "declared_size"}
