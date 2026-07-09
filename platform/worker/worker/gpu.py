"""GPU serialization + status (spec §18). Single global semaphore — one GPU job at a time.

With Arq max_jobs=1 the whole run is already serialized; this Redis flag makes GPU-in-use visible
to the dashboard, enforces the admin pause-queue control, and is the seam for finer scheduling later.
"""
from __future__ import annotations

import asyncio
import subprocess

from .config import wsettings


async def wait_if_paused(redis) -> None:
    """Block while an admin has paused the queue (§18)."""
    while await redis.get(wsettings.queue_paused_key):
        await asyncio.sleep(5)


class gpu_lease:
    """Async context manager: marks the GPU in-use for the duration of a job."""

    def __init__(self, redis, run_id: str) -> None:
        self.redis = redis
        self.run_id = run_id

    async def __aenter__(self):
        await self.redis.set(wsettings.gpu_lock_key, self.run_id)
        return self

    async def __aexit__(self, *exc):
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
