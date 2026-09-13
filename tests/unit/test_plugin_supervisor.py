import asyncio
import json
import sys
import time
import os
import tempfile
from uuid import uuid4

import pytest

from musicdl.contracts.plugin import PluginInvocation, PluginManifest, PluginRequest
from musicdl_plugin_runner.supervisor import Supervisor, HostCommand


def invocation(source="ok", timeout_ms=1000):
    import hashlib
    manifest = PluginManifest(plugin_id="p", version="1", language="python", operations=("search",), sha256=hashlib.sha256(source.encode()).hexdigest())
    return PluginInvocation(manifest=manifest, source=source, request=PluginRequest(protocol="musicdl.plugin/v1", request_id=uuid4(), operation="search", timeout_ms=timeout_ms))


def child(code):
    return [sys.executable, "-c", code]


def run_supervisor(code, timeout_ms=1000):
    async def run():
        return await Supervisor(command_builder=lambda inv: child(code)).execute(invocation(timeout_ms=timeout_ms))
    return asyncio.run(run())


def test_valid_json_and_nonzero_are_stable():
    async def run():
        sup = Supervisor(command_builder=lambda inv: child("print('{}')"))
        step = await sup.execute(invocation())
        assert step.response and step.response.ok is False
    asyncio.run(run())


def test_valid_plugin_step_and_invalid_output():
    from musicdl.contracts.plugin import PluginResponse
    response = PluginResponse(protocol="musicdl.plugin/v1", request_id=uuid4(), operation="search", ok=True, result={"hits": []})
    raw = json.dumps({"response": json.loads(response.model_dump_json()), "action": None})
    step = run_supervisor(f"import sys; sys.stdout.write({raw!r})")
    assert step.response and step.response.ok
    assert run_supervisor("print('not-json')").response.error.code == "invalid_output"


def test_nonzero_hides_stderr():
    step = run_supervisor("import sys; sys.stderr.write('stderr-canary'); sys.exit(3)")
    assert step.response.error.code == "plugin_failed"
    assert "stderr-canary" not in step.response.error.message


def test_spawn_timeout_maps_to_timeout(monkeypatch):
    async def timeout(*args, **kwargs):
        raise asyncio.TimeoutError
    monkeypatch.setattr(asyncio, "create_subprocess_exec", timeout)
    step = run_supervisor("print('unused')")
    assert step.response.error.code == "timeout"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_external_cancellation_reaps_child():
    async def run():
        sup = Supervisor(command_builder=lambda inv: child("import time; time.sleep(10)"))
        task = asyncio.create_task(sup.execute(invocation(timeout_ms=5000)))
        await asyncio.sleep(.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())


def test_rejects_third_job_immediately():
    async def run():
        sup = Supervisor(command_builder=lambda inv: child("import time; time.sleep(1)"), max_concurrency=2)
        tasks = [asyncio.create_task(sup.execute(invocation(timeout_ms=2000))) for _ in range(2)]
        await asyncio.sleep(.05)
        start = time.monotonic()
        third = await sup.execute(invocation())
        assert time.monotonic() - start < .2
        assert third.response and third.response.error.code == "busy"
        await asyncio.gather(*tasks)
    asyncio.run(run())


def test_timeout_returns_sanitized_error():
    async def run():
        sup = Supervisor(command_builder=lambda inv: child("import time; time.sleep(2)"))
        step = await sup.execute(invocation(timeout_ms=50))
        assert step.response and step.response.error.code == "timeout"
        assert "time.sleep" not in step.response.error.message
    asyncio.run(run())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_term_trap_reaches_kill_after_grace():
    started = time.monotonic()
    step = run_supervisor("import signal,time; signal.signal(signal.SIGTERM, lambda *_: None); time.sleep(2)", timeout_ms=80)
    elapsed = time.monotonic() - started
    assert step.response.error.code == "timeout"
    assert elapsed >= .25


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_descendant_group_is_killed():
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        pid_file = handle.name
    try:
        code = ("import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',"
                f"'import os,time; open({pid_file!r},\\\"w\\\").write(str(os.getpid())); time.sleep(10)']); time.sleep(10)")
        step = run_supervisor(code, timeout_ms=100)
        assert step.response.error.code == "timeout"
        pid = None
        for _ in range(20):
            try:
                text = open(pid_file, encoding="ascii").read()
                if text: pid = int(text); break
            except (FileNotFoundError, ValueError):
                time.sleep(.02)
        assert pid is not None
        for _ in range(20):
            try: os.kill(pid, 0)
            except ProcessLookupError: break
            time.sleep(.02)
        else: pytest.fail("descendant process survived group cleanup")
    finally:
        try: os.unlink(pid_file)
        except FileNotFoundError: pass


def test_stdout_and_stderr_overflow_are_bounded():
    stdout_step = run_supervisor("import sys; sys.stdout.write('x'*65537)")
    stderr_step = run_supervisor("import sys; sys.stderr.write('x'*16385)")
    assert stdout_step.response.error.code == "output_too_large"
    assert stderr_step.response.error.code == "output_too_large"


def test_reader_io_failure_is_sanitized_and_cleaned_up(monkeypatch):
    async def failing_reader(stream, limit):
        raise OSError("attacker-controlled reader failure")

    async def run():
        sup = Supervisor(command_builder=lambda inv: child("import time; time.sleep(10)"))
        monkeypatch.setattr(sup, "_read_bounded", failing_reader)
        step = await sup.execute(invocation(timeout_ms=1000))
        await asyncio.sleep(0)
        assert step.response and step.response.error.code == "io_failed"
        assert "attacker-controlled" not in step.response.error.message
        assert sup._active == 0

    asyncio.run(run())

def test_host_command_passes_custom_stdin_and_environment_and_cleans_cwd():
    async def run():
        def build(inv):
            code=("import os,sys; data=sys.stdin.read(); assert os.environ.get('MARK')=='ok' and data=='payload'; "
                  "sys.stdout.write('{\"response\":{\"protocol\":\"musicdl.plugin/v1\",\"request_id\":\"00000000-0000-0000-0000-000000000000\",\"operation\":\"search\",\"ok\":true,\"result\":{\"ok\":true}},\"action\":null}')")
            return HostCommand(child(code), {"MARK":"ok"}, b"payload")
        sup=Supervisor(command_builder=build)
        step=await sup.execute(invocation())
        return step
    step=asyncio.run(run())
    assert step.response.ok and step.response.result == {"ok":True}

def test_spawn_failure_releases_slot_and_sanitizes_error():
    async def run():
        sup=Supervisor(command_builder=lambda inv: ["definitely-not-a-real-host"])
        step=await sup.execute(invocation())
        assert sup._active == 0
        return step
    step=asyncio.run(run())
    assert step.response.error.code == "spawn_failed"

def test_job_directory_is_removed_after_success_and_spawn_failure(monkeypatch):
    removed=[]
    monkeypatch.setattr("musicdl_plugin_runner.supervisor.shutil.rmtree", lambda path: removed.append(path))
    async def run():
        good=Supervisor(command_builder=lambda inv: child("print('bad')"))
        bad=Supervisor(command_builder=lambda inv: ["missing-plugin-host"])
        await good.execute(invocation()); await bad.execute(invocation())
    asyncio.run(run())
    assert len(removed) == 2 and all(path.startswith(tempfile.gettempdir()) for path in removed)

def test_cleanup_failure_is_sanitized(monkeypatch):
    monkeypatch.setattr("musicdl_plugin_runner.supervisor._remove_job_dir", lambda path: False)
    step=run_supervisor("print('unused')")
    assert step.response.error.code == "cleanup_failed"
    assert tempfile.gettempdir() not in step.response.error.message

def test_cleanup_state_is_request_local(monkeypatch):
    seen=[]
    def remove(path):
        seen.append(path)
        return len(seen) != 1
    monkeypatch.setattr("musicdl_plugin_runner.supervisor._remove_job_dir", remove)
    async def run():
        sup=Supervisor(command_builder=lambda inv: child("import time; time.sleep(.05); print('bad')"), max_concurrency=2)
        return await asyncio.gather(sup.execute(invocation()), sup.execute(invocation()))
    steps=asyncio.run(run())
    codes=[s.response.error.code for s in steps]
    assert codes.count("cleanup_failed") == 1
    assert codes.count("invalid_output") == 1
