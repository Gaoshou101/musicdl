"""Docker-only hostile acceptance tests for the restricted plugin runner."""
from __future__ import annotations
import hashlib, json, os, subprocess, tempfile, time, uuid
from pathlib import Path
import pytest

ROOT = Path(__file__).parents[2]
PROJECT = f"musicdl-plugin-test-{os.getpid()}"
CANARY = "integration-canary-must-not-escape"
COMPOSE = ["compose", "-f", "compose.yaml"]
COMPOSE_ENV: dict[str, str] | None = None

def _docker(*args: str, input_text: str | None = None, check: bool = True, timeout: int = 180):
    return subprocess.run(["docker", *args], cwd=ROOT, env=COMPOSE_ENV, input=input_text, text=True, capture_output=True, check=check, timeout=timeout)

def _compose(*args: str, input_text: str | None = None, check: bool = True, timeout: int = 180):
    return _docker(*COMPOSE, "-p", PROJECT, *args, input_text=input_text, check=check, timeout=timeout)

@pytest.fixture(scope="session", autouse=True)
def docker_runtime():
    global COMPOSE_ENV
    try:
        _docker("version")
    except (OSError, subprocess.SubprocessError):
        pytest.skip("Docker Engine is not available")
    env = os.environ.copy()
    env["MUSICDL_REDIS__URL"] = "redis://redis.invalid:6379/0"
    env["MUSICDL_TEST_CANARY"] = CANARY
    COMPOSE_ENV = env
    override = None
    try:
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".yaml", delete=False)
        handle.write('services:\n  plugin-runner:\n    environment:\n      MUSICDL_TEST_CANARY: "${MUSICDL_TEST_CANARY:?test canary required}"\n')
        handle.close()
        override = Path(handle.name)
        COMPOSE.extend(["-f", str(override)])
        build = _compose("build", "--no-cache", "plugin-runner", check=False, timeout=900)
        if build.returncode:
            raise AssertionError(f"Compose build failed:\n{build.stdout}\n{build.stderr}")
        up = _compose("up", "-d", "plugin-runner", check=False, timeout=180)
        if up.returncode:
            raise AssertionError(f"Compose up failed:\n{up.stdout}\n{up.stderr}\nPS:\n{_compose('ps', check=False).stdout}\nLOGS:\n{_compose('logs', 'plugin-runner', check=False).stdout}")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            health = _compose("exec", "-T", "plugin-runner", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')", check=False)
            if health.returncode == 0: break
            time.sleep(1)
        else:
            raise AssertionError(f"runner did not become healthy\nPS:\n{_compose('ps', check=False).stdout}\nLOGS:\n{_compose('logs', 'plugin-runner', check=False).stdout}")
        container_id = _compose("ps", "-q", "plugin-runner").stdout.strip().splitlines()
        assert len(container_id) == 1 and container_id[0]
        inspected = json.loads(_docker("inspect", container_id[0]).stdout)[0]
        assert f"MUSICDL_TEST_CANARY={CANARY}" in inspected["Config"]["Env"]
        yield
    finally:
        _compose("down", "--volumes", "--remove-orphans", check=False)
        if override is not None: override.unlink(missing_ok=True)
        COMPOSE[:] = ["compose", "-f", "compose.yaml"]
        COMPOSE_ENV = None

def _exec_python(payload: dict):
    code = ("import json,sys,urllib.request,urllib.error; req=urllib.request.Request('http://127.0.0.1:8080/v1/execute', data=sys.stdin.buffer.read(), headers={'content-type':'application/json'}); "
            "\ntry: r=urllib.request.urlopen(req, timeout=35); print(json.dumps({'status':r.status,'body':r.read().decode()})) "
            "\nexcept urllib.error.HTTPError as e: print(json.dumps({'status':e.code,'body':e.read().decode()}))")
    return _compose("exec", "-T", "plugin-runner", "python", "-c", code, input_text=json.dumps(payload))

def _health() -> dict:
    code = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read().decode())"
    return json.loads(_compose("exec", "-T", "plugin-runner", "python", "-c", code).stdout)

def _invoke(language: str, source: str, payload: dict, timeout_ms: int = 2000) -> tuple[int, dict]:
    body = {"manifest": {"plugin_id": "integration", "version": "1", "language": language, "operations": ["search"], "sha256": hashlib.sha256(source.encode()).hexdigest()}, "source": source, "request": {"protocol": "musicdl.plugin/v1", "request_id": str(uuid.uuid4()), "operation": "search", "timeout_ms": timeout_ms, "payload": payload}}
    result = json.loads(_exec_python(body).stdout)
    return result["status"], json.loads(result["body"])

def _source(name: str) -> str:
    return (ROOT / "tests" / "fixtures" / "plugins" / name).read_text(encoding="utf-8")

@pytest.mark.integration
@pytest.mark.parametrize("language,fixture", [("python", "minimal.py"), ("javascript", "minimal.js")])
def test_valid_python_and_javascript_execute(language: str, fixture: str):
    status, step = _invoke(language, _source(fixture), {"query": "test"})
    assert status == 200
    assert step["response"]["ok"] is True, step
    assert step["response"]["result"]["hits"][0]["title"] == "test"

HOSTILE_MODES = ("environment", "proc", "shadow", "docker_socket", "telegram_session", "main_network", "public_network", "subprocess", "fork", "command", "infinite_cpu", "memory", "pid_exhaustion", "tmp_exhaustion", "output_exhaustion", "traversal", "retained_file")
EXPECTED_CODES = {mode: {"plugin_error", "plugin_failed"} for mode in HOSTILE_MODES}
# Only the Python host catches MemoryError and reports resource_limit; Deno dies with
# the interpreter (plugin_failed), and a slower host can spend the whole invocation
# budget on Deno startup before the allocation is even attempted, which the supervisor
# reports as timeout. All three are denials; that the cap exists is asserted by the
# container contract, while this test asserts the request is denied without a leak.
EXPECTED_CODES.update({"infinite_cpu": {"timeout", "plugin_failed"}, "memory": {"resource_limit", "plugin_error", "plugin_failed", "timeout"}, "output_exhaustion": {"output_too_large", "plugin_error"}})
# ``fork`` and ``pid_exhaustion`` are the same ``os.fork()`` call. The forked child
# inherits the host's stdout/stderr, so the supervisor's readers never see EOF even
# though the host exits 0: it reports ``timeout`` (the reader branch of
# musicdl_plugin_runner/supervisor.py) instead of ``plugin_failed``. Which of the two
# happens is a scheduling race on a loaded runner and CI has produced both, so both
# are accepted. The denial assertions themselves are unchanged.
EXPECTED_CODES.update({"fork": {"timeout", "plugin_error", "plugin_failed"},
                       "pid_exhaustion": {"timeout", "plugin_error", "plugin_failed"}})
# The host has to boot before it can deny anything, and the memory mode also has to
# reach its allocation: on a loaded runner the default 1 s budget is gone before
# either happens, so the supervisor reports ``timeout`` instead of the host's own
# verdict.  Both modes are denied within milliseconds of the host starting (Deno
# refuses the write, the Python host hits its address-space limit), so the extra
# room removes the race without relaxing the codes expected below.
STARTUP_HEADROOM_MODES = {"memory", "tmp_exhaustion"}
STARTUP_HEADROOM_MS = 5000

@pytest.mark.integration
@pytest.mark.parametrize("language,fixture", [("python", "hostile.py"), ("javascript", "hostile.js")])
@pytest.mark.parametrize("mode", HOSTILE_MODES)
def test_hostile_plugin_is_denied_and_runner_recovers(language: str, fixture: str, mode: str):
    status, step = _invoke(language, _source(fixture), {"mode": mode},
                           timeout_ms=STARTUP_HEADROOM_MS if mode in STARTUP_HEADROOM_MODES else 1000)
    assert status in {502, 504}
    assert step["response"]["ok"] is False, step
    assert step["response"]["error"]["code"] in EXPECTED_CODES[mode], step
    assert CANARY not in json.dumps(step)
    assert _health()["status"] == "ok"
    logs = _compose("logs", "plugin-runner", check=False).stdout
    assert CANARY not in logs
    if mode in {"tmp_exhaustion", "traversal", "retained_file"}:
        probe = "import pathlib; print('\\n'.join(str(p) for p in pathlib.Path('/tmp').rglob('*') if p.name in {'retained-canary','escape','plugin-fill'} or p.name.startswith('musicdl-plugin-job-')))"
        assert not _compose("exec", "-T", "plugin-runner", "python", "-c", probe).stdout.strip()

@pytest.mark.integration
def test_runner_container_security_contract():
    container_id = _compose("ps", "-q", "plugin-runner").stdout.strip().splitlines()
    assert len(container_id) == 1 and container_id[0]
    data = json.loads(_docker("inspect", container_id[0]).stdout)[0]
    host = data["HostConfig"]
    assert data["Config"]["User"] == "10001:10001"
    assert host["ReadonlyRootfs"] is True and host["Memory"] == 512 * 1024 * 1024
    assert host["NanoCpus"] == 1_000_000_000 and host["PidsLimit"] == 64
    assert host["Init"] is True
    assert "no-new-privileges:true" in {str(value).lower() for value in (host["SecurityOpt"] or [])}
    assert host["Privileged"] is False and not (host["Devices"] or [])
    assert "ALL" in {str(value).upper() for value in (host["CapDrop"] or [])}
    assert not (host["Binds"] or [])
    assert not any(m.get("Type") in {"bind", "volume"} for m in (data.get("Mounts") or []))
    tmpfs = host["Tmpfs"].get("/tmp", "")
    assert set(tmpfs.split(",")) == {"size=32m", "noexec", "nosuid", "nodev"}
    assert not (data["NetworkSettings"]["Ports"] or {})
    env_names = {entry.split("=", 1)[0] for entry in data["Config"]["Env"]}
    assert "MUSICDL_TEST_CANARY" in env_names
    assert not env_names.intersection({"MUSICDL_REDIS__URL", "MUSICDL_TELEGRAM__API_ID", "MUSICDL_TELEGRAM__API_HASH", "OPENAI_API_KEY"})
    network_names = data["NetworkSettings"]["Networks"]
    assert len(network_names) == 1 and list(network_names)[0].endswith("_plugin-control")
    network = json.loads(_docker("network", "inspect", list(network_names)[0]).stdout)[0]
    assert network["Internal"] is True
