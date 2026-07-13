"""GPU serialization + status (spec §18). Single global GPU semaphore — one GPU job at a time.

Arq runs up to `max_jobs` runs concurrently (§18), so GPU exclusivity is NOT provided by the queue;
it is enforced here by `gpu_lease`, a real blocking Redis mutex. Only GPU detectors take the lease
(`runner.uses_gpu`), so a CPU-only detector (e.g. HippUnfold) runs alongside a GPU job instead of
queuing behind it. The lock's value is the holding run_id, which the dashboard reads to show what is
on the GPU; a TTL frees it if a worker dies mid-job.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import subprocess
from collections.abc import Awaitable, Callable

from arq import Retry

from .config import wsettings

_POLL_S = 2.0
_COMPARE_DELETE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
_COMPARE_EXPIRE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


async def wait_if_paused(redis) -> None:
    """Defer queued work while paused without consuming its execution timeout."""
    if await redis.get(wsettings.queue_paused_key):
        raise Retry(defer=60)


class gpu_lease:
    """Async context manager: a blocking single-holder GPU mutex (spec §18).

    Acquire waits (polling `SET NX`) until the one GPU slot is free, then holds it. Ownership uses
    both run ID and claim token, preventing an expired attempt from deleting a retry's lease."""

    def __init__(self, redis, run_id: str, claim_token: str,
                 *, ttl_s: int | None = None,
                 wait_check: Callable[[], Awaitable[None]] | None = None) -> None:
        self.redis = redis
        self.run_id = run_id
        self.owner = f"{run_id}:{claim_token}"
        self.ttl_s = ttl_s
        self.wait_check = wait_check
        self.renewal_task: asyncio.Task | None = None
        self.owner_task: asyncio.Task | None = None

    async def _renew(self, ttl_s: int) -> None:
        while True:
            await asyncio.sleep(max(5, ttl_s // 3))
            try:
                renewed = await self.redis.eval(
                    _COMPARE_EXPIRE, 1, wsettings.gpu_lock_key, self.owner, ttl_s)
            except Exception:
                renewed = 0
            if renewed != 1:
                if self.owner_task is not None:
                    self.owner_task.cancel()
                return

    async def __aenter__(self):
        # A short crash TTL is continuously owner-fenced and renewed. Losing Redis ownership
        # cancels the owning task before another job can safely acquire the physical GPU.
        lock_ttl_s = self.ttl_s or max(300, wsettings.run_claim_lease_s * 2)
        if self.wait_check is not None:
            await self.wait_check()
        while not await self.redis.set(
                wsettings.gpu_lock_key, self.owner, nx=True, ex=lock_ttl_s):
            if self.wait_check is not None:
                await self.wait_check()
            await asyncio.sleep(_POLL_S)
        self.owner_task = asyncio.current_task()
        self.renewal_task = asyncio.create_task(
            self._renew(lock_ttl_s), name=f"gpu-lease-renewal:{self.run_id}")
        return self

    async def __aexit__(self, *exc):
        if self.renewal_task is not None:
            self.renewal_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.renewal_task
        # One server-side operation: a TTL expiry/successor acquisition cannot occur between the
        # ownership comparison and delete.
        await self.redis.eval(_COMPARE_DELETE, 1, wsettings.gpu_lock_key, self.owner)


def gpu_status() -> dict:
    """nvidia-smi snapshot for /api/system."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        used, total, util = (int(x.strip()) for x in out.split(","))
        return {"vram_used_mib": used, "vram_total_mib": total, "gpu_util_pct": util}
    except Exception:
        return {"vram_used_mib": None, "vram_total_mib": None, "gpu_util_pct": None}
