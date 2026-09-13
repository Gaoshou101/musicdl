"""Bounded subprocess supervision for untrusted plugin hosts."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from collections.abc import Awaitable, Callable
from typing import Any

from musicdl.contracts.plugin import MAX_INVOCATION_BYTES, PluginInvocation, PluginStep

MAX_STDOUT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 16 * 1024
GRACE_SECONDS = 0.25


class Supervisor:
    def __init__(self, command_builder: Callable[[PluginInvocation], list[str]], max_concurrency: int = 2):
        self.command_builder = command_builder
        self.max_concurrency = max_concurrency
        self._active = 0
        self._lock = asyncio.Lock()

    async def _claim(self) -> bool:
        async with self._lock:
            if self._active >= self.max_concurrency:
                return False
            self._active += 1
            return True

    async def _release(self) -> None:
        async with self._lock:
            self._active -= 1

    @staticmethod
    def _error(code: str, message: str) -> PluginStep:
        from musicdl.contracts.plugin import PluginError, PluginResponse
        from uuid import UUID
        # A synthetic response is replaced with request metadata by execute.
        return PluginStep(response=PluginResponse(protocol="musicdl.plugin/v1", request_id=UUID(int=0), operation="health", ok=False, error=PluginError(code=code, message=message)))

    @staticmethod
    def _with_request(step: PluginStep, invocation: PluginInvocation) -> PluginStep:
        if step.response is None:
            return step
        response = step.response.model_copy(update={"request_id": invocation.request.request_id, "operation": invocation.request.operation})
        return PluginStep(response=response)

    @staticmethod
    async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
        data = bytearray()
        overflow = False
        while True:
            chunk = await stream.read(min(8192, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > limit:
                overflow = True
                break
        return bytes(data[:limit]), overflow

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=GRACE_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass
        await proc.wait()

    async def execute(self, invocation: PluginInvocation) -> PluginStep:
        if not await self._claim():
            return self._with_request(self._error("busy", "plugin runner is busy"), invocation)
        proc: asyncio.subprocess.Process | None = None
        try:
            try:
                encoded = invocation.model_dump_json().encode()
            except Exception:
                return self._with_request(self._error("invalid_invocation", "invalid invocation"), invocation)
            if len(encoded) > MAX_INVOCATION_BYTES:
                return self._with_request(self._error("invocation_too_large", "invocation exceeds limit"), invocation)
            try:
                command = self.command_builder(invocation)
                if not command or any(not isinstance(part, str) for part in command):
                    raise ValueError
                kwargs: dict[str, Any] = {"stdin": asyncio.subprocess.PIPE, "stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE, "env": {}, "close_fds": True}
                if os.name == "posix":
                    kwargs["start_new_session"] = True
                else:
                    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                proc = await asyncio.create_subprocess_exec(*command, **kwargs)
            except Exception:
                return self._with_request(self._error("spawn_failed", "plugin host unavailable"), invocation)
            assert proc.stdin and proc.stdout and proc.stderr
            proc.stdin.write(encoded)
            await proc.stdin.drain()
            proc.stdin.close()
            stdout_task = asyncio.create_task(self._read_bounded(proc.stdout, MAX_STDOUT_BYTES))
            stderr_task = asyncio.create_task(self._read_bounded(proc.stderr, MAX_STDERR_BYTES))
            try:
                (stdout, stdout_over), (_, stderr_over) = await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task), invocation.request.timeout_ms / 1000)
                await asyncio.wait_for(proc.wait(), invocation.request.timeout_ms / 1000)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                await self._terminate(proc)
                stdout_task.cancel(); stderr_task.cancel()
                return self._with_request(self._error("timeout", "plugin execution timed out"), invocation)
            if stdout_over or stderr_over:
                await self._terminate(proc)
                return self._with_request(self._error("output_too_large", "plugin output exceeds limit"), invocation)
            if proc.returncode != 0:
                return self._with_request(self._error("plugin_failed", "plugin execution failed"), invocation)
            try:
                return self._with_request(PluginStep.model_validate_json(stdout), invocation)
            except Exception:
                return self._with_request(self._error("invalid_output", "plugin returned invalid output"), invocation)
        finally:
            if proc is not None and proc.returncode is None:
                await self._terminate(proc)
            await self._release()
