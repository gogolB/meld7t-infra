"""GPU serialization + status (spec §18). Single global GPU semaphore — one GPU job at a time.

Arq runs up to `max_jobs` runs concurrently (§18), so GPU exclusivity is NOT provided by the queue;
it is enforced here by `gpu_lease`, a real blocking Redis mutex. Only GPU detectors take the lease
(`runner.uses_gpu`), so a CPU-only detector (e.g. HippUnfold) runs alongside a GPU job instead of
queuing behind it. The lock's value is the holding run_id, which the dashboard reads to show what is
on the GPU; a TTL frees it if a worker dies mid-job.
"""
from __future__ import annotations

import asyncio
import subprocess

from .config import wsettings

# TTL > Arq job_timeout (4 h): a crashed holder's lock self-heals, but a live job is killed by the
# timeout long before its lock could expire — so two GPU jobs can never overlap in normal operation.
_LOCK_TTL_S = 5 * 60 * 60
_POLL_S = 2.0


async def wait_if_paused(redis) -> None:
    """Block while an admin has paused the queue (§18)."""
    while await redis.get(wsettings.queue_paused_key):
        await asyncio.sleep(5)


class gpu_lease:
    """Async context manager: a blocking single-holder GPU mutex (spec §18).

    Acquire waits (polling `SET NX`) until the one GPU slot is free, then holds it — keyed to
    run_id so the dashboard shows the current GPU holder. Release only clears the lock if we still
    own it, so a successor that acquired after a TTL expiry is never clobbered."""

    def __init__(self, redis, run_id: str) -> None:
        self.redis = redis
        self.run_id = run_id

    async def __aenter__(self):
        while not await self.redis.set(
                wsettings.gpu_lock_key, self.run_id, nx=True, ex=_LOCK_TTL_S):
            await asyncio.sleep(_POLL_S)
        return self

    async def __aexit__(self, *exc):
        cur = await self.redis.get(wsettings.gpu_lock_key)
        if cur is not None and (cur.decode() if isinstance(cur, bytes) else cur) == self.run_id:
            await self.redis.delete(wsettings.gpu_lock_key)


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
