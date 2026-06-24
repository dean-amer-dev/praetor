"""asyncpg connection pool — shared by webhook-adapter and workers.

Pool is lazily initialized on first use and shared within a process.
Set PRAETOR_DB_URL to a postgres:// or postgresql:// DSN.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_pool = None


async def get_pool():
    """Return the shared asyncpg pool, creating it on first call."""
    global _pool
    if _pool is not None:
        return _pool

    dsn = os.environ.get("PRAETOR_DB_URL", "")
    if not dsn:
        return None

    try:
        import asyncpg  # type: ignore[import]
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        logger.info("praetor db pool created")
    except Exception as exc:
        logger.warning("praetor db pool creation failed: %s", exc)
        _pool = None

    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
