"""Cancellation-safe subprocess execution for sibling compute containers.

ARQ cancels a coroutine when its job deadline is reached.  A plain ``await proc.wait()`` leaves
the Podman client (and sometimes its container) running after that cancellation.  All worker
commands go through this module so cancellation/timeout terminates the complete process group and
explicitly removes a named container before the GPU lease can be released.
"""
from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass

from .config import wsettings


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes = b""


def _container_name(cmd: list[str]) -> str | None:
    if not cmd or os.path.basename(cmd[0]) != "podman":
        return None
    try:
        index = cmd.index("--name")
        return cmd[index + 1]
    except (ValueError, IndexError):
        return None


async def _force_remove_container(name: str | None) -> None:
    if not name:
        return
    try:
        cleanup = await asyncio.create_subprocess_exec(
            "podman", "rm", "--force", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        await asyncio.wait_for(cleanup.wait(), timeout=30)
    except (Exception, asyncio.CancelledError):
        # Cleanup is best effort after the process group has already been killed.  Never mask the
        # original detector failure/cancellation with a Podman bookkeeping error.
        pass


async def _terminate(proc: asyncio.subprocess.Process, cmd: list[str]) -> None:
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=wsettings.subprocess_stop_grace_s)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()
    await _force_remove_container(_container_name(cmd))


async def run_process(
    cmd: list[str],
    log_path: str,
    *,
    capture_stdout: bool = False,
    timeout_s: int | None = None,
    display_cmd: list[str] | None = None,
) -> ProcessResult:
    """Run ``cmd`` with a hard timeout and cancellation-safe process/container cleanup."""
    timeout = timeout_s if timeout_s is not None else wsettings.subprocess_timeout_s
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "ab") as log:
        log.write(("$ " + " ".join(display_cmd or cmd) + "\n").encode())
        log.flush()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE if capture_stdout else log,
            stderr=log,
            start_new_session=True,
        )
        try:
            if capture_stdout:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            else:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
                stdout = b""
        except asyncio.TimeoutError:
            await _terminate(proc, cmd)
            raise TimeoutError(f"command exceeded {timeout}s: {cmd[0]}") from None
        except asyncio.CancelledError:
            # Shield cleanup so a second cancellation cannot release the GPU lease while the
            # actual container is still executing.
            await asyncio.shield(_terminate(proc, cmd))
            raise
    return ProcessResult(proc.returncode or 0, stdout)
