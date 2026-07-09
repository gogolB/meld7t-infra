"""Redis/Arq access for the API — enqueue runs, read live status (spec §5.2, §18)."""
from __future__ import annotations

import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import RedisSettings

from .config import settings

GPU_INUSE_KEY = "meld7t:gpu:inuse"
QUEUE_PAUSED_KEY = "meld7t:queue:paused"

_pool = None
_redis = None


async def get_pool():
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _pool


def get_redis():
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def enqueue_run(run_id: str) -> None:
    pool = await get_pool()
    await pool.enqueue_job("run_detector", run_id)
