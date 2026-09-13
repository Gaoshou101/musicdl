"""Docker-only hostile acceptance tests for the restricted plugin runner."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
PROJECT = f"musicdl-plugin-test-{os.getpid()}"
CANARY = "integration-canary-must-not-escape"


def _docker(*args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], cwd=ROOT, input=input_text, text=True,
        capture_output=True, check=check, timeout=180,
    )


@pytest.fixture(scope="session", autouse=True)
def docker_runtime():
    """Build and own one isolated Compose project for the whole acceptance run."""
    try:
        _docker("version")
    except (OSError, subprocess.SubprocessError):
        pytest.skip("Docker Engine is not available")

    env = os.environ.copy()
    env["MUSICDL_REDIS__URL"] = "redis://redis.invalid:6379/0"
    env["MUSICDL_CANARY"] = CANARY
    base = ["compose", "-p", PROJECT]
    try:
        subprocess.run(["docker", *base, "build", "--no-cache", "plugin-runner"], cwd=ROOT, env=env,
                       check=True, capture_output=True, text=True, timeout=900)
        subprocess.run(["docker", *base, "up", "-d", "plugin-runner"], cwd=ROOT, env=env,
                       check=True, capture_output=True, text=True, timeout=180)
        yield
    finally:
        subprocess.run(["docker", *base, "down", "--volumes", "--remove-orphans"], cwd=ROOT, env=env,
                       check=False, capture_output=True, text=True, timeout=180)


def _exec_python(script: str, payload: dict) -> subprocess.CompletedProcess[str]:
    code = (
        "import json,sys,urllib.request; "
        "req=urllib.request.Request('http://127.0.0.1:8080/v1/execute', "
        "data=sys.stdin.buffer.read(), headers={'content-type':'application/json'}); "
        "print(urllib.request.urlopen(req, timeout=35).read().decode())"
    )
    return _docker("compose", "-p", PROJECT, "exec", "-T", "plugin-runner", "python", "-c", code,
                   input_text=json.dumps(payload))


def _health() -> dict:
    code = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read().decode())"
    return json.loads(_docker("compose", "-p", PROJECT, "exec", "-T", "plugin-runner", "python", "-c", code).stdout)


def _invoke(language: str, source: str, payload: dict, timeout_ms: int = 2000) -> dict:
    digest = hashlib.sha256(source.encode()).hexdigest()
    body = {
        "manifest": {"plugin_id": "integration", "version": "1", "language": language,
                     "operations": ["search"], "sha256": digest},
        "source": source,
        "request": {"protocol": "musicdl.plugin/v1", "request_id": str(uuid.uuid4()),
                    "operation": "search", "timeout_ms": timeout_ms, "payload": payload},
    }
    return json.loads(_exec_python("", body).stdout)


def _source(name: str) -> str:
    return (ROOT / "tests" / "fixtures" / "plugins" / name).read_text(encoding="utf-8")


@pytest.mark.integration
@pytest.mark.parametrize("language,fixture", [("python", "minimal.py"), ("javascript", "minimal.js")])
def test_valid_python_and_javascript_execute(language: str, fixture: str):
    response = _invoke(language, _source(fixture), {"query": "test"})
    assert response["response"]["response"]["ok"] is True, response
    assert response["response"]["response"]["result"]["hits"][0]["title"] == "test"


HOSTILE_MODES = (
    "environment", "proc", "shadow", "docker_socket", "telegram_session",
    "main_network", "public_network", "subprocess", "fork", "command",
    "infinite_cpu", "memory", "pid_exhaustion", "tmp_exhaustion",
    "output_exhaustion", "traversal", "retained_file",
)


@pytest.mark.integration
@pytest.mark.parametrize("language,fixture", [("python", "hostile.py"), ("javascript", "hostile.js")])
@pytest.mark.parametrize("mode", HOSTILE_MODES)
def test_hostile_plugin_is_denied_and_runner_recovers(language: str, fixture: str, mode: str):
    response = _invoke(language, _source(fixture), {"mode": mode}, timeout_ms=1000)
    text = json.dumps(response)
    assert response["response"]["response"]["ok"] is False, response
    assert CANARY not in text
    assert _health()["status"] == "ok"
    logs = _docker("compose", "-p", PROJECT, "logs", "plugin-runner", check=False).stdout
    assert CANARY not in logs
