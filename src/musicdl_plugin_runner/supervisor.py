"""Bounded subprocess supervision for untrusted plugin hosts."""
from __future__ import annotations

import asyncio
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
        except asyncio.TimeoutError:
            pass
        try:
            if os.name == "posix":
                # The leader may have exited while descendants retain the pgid.
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    return
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass
        await proc.wait()

    @staticmethod
    async def _cancel_readers(tasks: list[asyncio.Task]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def execute(self, invocation: PluginInvocation) -> PluginStep:
        if not await self._claim():
            return self._with_request(self._error("busy", "plugin runner is busy"), invocation)
        proc: asyncio.subprocess.Process | None = None
        readers: list[asyncio.Task] = []
        deadline = asyncio.get_running_loop().time() + invocation.request.timeout_ms / 1000
        try:
            try:
                encoded = invocation.model_dump_json().encode()
            except asyncio.TimeoutError:
                return self._with_request(self._error("timeout", "plugin execution timed out"), invocation)
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
                proc = await asyncio.wait_for(asyncio.create_subprocess_exec(*command, **kwargs), max(0, deadline - asyncio.get_running_loop().time()))
            except Exception:
                return self._with_request(self._error("spawn_failed", "plugin host unavailable"), invocation)
            assert proc.stdin and proc.stdout and proc.stderr
            try:
                proc.stdin.write(encoded)
                await asyncio.wait_for(proc.stdin.drain(), max(0, deadline - asyncio.get_running_loop().time()))
                proc.stdin.close()
            except asyncio.TimeoutError:
                await self._terminate(proc)
                return self._with_request(self._error("timeout", "plugin execution timed out"), invocation)
            except (BrokenPipeError, ConnectionError, OSError):
                await self._terminate(proc)
                return self._with_request(self._error("io_failed", "plugin I/O failed"), invocation)
            readers = [asyncio.create_task(self._read_bounded(proc.stdout, MAX_STDOUT_BYTES)), asyncio.create_task(self._read_bounded(proc.stderr, MAX_STDERR_BYTES))]
            wait_task = asyncio.create_task(proc.wait())
            try:
                done, _ = await asyncio.wait_for(asyncio.wait(readers + [wait_task], return_when=asyncio.FIRST_COMPLETED), max(0, deadline - asyncio.get_running_loop().time()))
                if any(task in done and not task.cancelled() and task.result()[1] for task in readers):
                    await self._terminate(proc)
                    await self._cancel_readers(readers)
                    return self._with_request(self._error("output_too_large", "plugin output exceeds limit"), invocation)
                await asyncio.wait_for(asyncio.gather(*readers), max(0, deadline - asyncio.get_running_loop().time()))
                if any(task.result()[1] for task in readers):
                    await self._terminate(proc)
                    await self._cancel_readers(readers + [wait_task])
                    return self._with_request(self._error("output_too_large", "plugin output exceeds limit"), invocation)
                await asyncio.wait_for(asyncio.shield(wait_task), max(0, deadline - asyncio.get_running_loop().time()))
                stdout = readers[0].result()[0]
            except asyncio.TimeoutError:
                await self._terminate(proc)
                await self._cancel_readers(readers + [wait_task])
                return self._with_request(self._error("timeout", "plugin execution timed out"), invocation)
            except asyncio.CancelledError:
                await asyncio.shield(self._terminate(proc))
                await asyncio.shield(self._cancel_readers(readers + [wait_task]))
                raise
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
