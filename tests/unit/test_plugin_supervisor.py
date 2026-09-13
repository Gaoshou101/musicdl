import asyncio
import json
import sys
import time
from uuid import uuid4

import pytest

from musicdl.contracts.plugin import PluginInvocation, PluginManifest, PluginRequest
from musicdl_plugin_runner.supervisor import Supervisor


def invocation(source="ok", timeout_ms=1000):
    import hashlib
    manifest = PluginManifest(plugin_id="p", version="1", language="python", operations=("search",), sha256=hashlib.sha256(source.encode()).hexdigest())
    return PluginInvocation(manifest=manifest, source=source, request=PluginRequest(protocol="musicdl.plugin/v1", request_id=uuid4(), operation="search", timeout_ms=timeout_ms))


def child(code):
    return [sys.executable, "-c", code]


def test_valid_json_and_nonzero_are_stable():
    async def run():
        sup = Supervisor(command_builder=lambda inv: child("print('{}')"))
        step = await sup.execute(invocation())
        assert step.response and step.response.ok is False
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
