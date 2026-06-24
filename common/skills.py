"""Skills system — per-agent prompt-only skill snippets backed by PostgreSQL + Langfuse.

Workers call assemble_prompt() at task-start so skills take effect without pod restarts.
"""
from __future__ import annotations

import logging

from .db import get_pool
from .langfuse_tools import get_system_prompt

logger = logging.getLogger(__name__)


async def load_skill_assignments(agent_name: str) -> list[str]:
    """Return skill names assigned to agent_name, ordered by assigned_at."""
    pool = await get_pool()
    if pool is None:
        return []
    try:
        rows = await pool.fetch(
            "SELECT skill_name FROM praetor_agent_skills WHERE agent_name = $1 ORDER BY assigned_at",
            agent_name,
        )
        return [r["skill_name"] for r in rows]
    except Exception as exc:
        logger.warning("load_skill_assignments(%s) failed: %s", agent_name, exc)
        return []


async def assemble_prompt(agent_name: str, base: str) -> str:
    """Return base prompt with any active skill snippets appended.

    Skill text is fetched from Langfuse (skill-{name}). Falls back gracefully
    if DB or Langfuse is unavailable — base prompt is always returned.
    """
    skill_names = await load_skill_assignments(agent_name)
    if not skill_names:
        return base

    snippets = [
        get_system_prompt(f"skill-{name}", fallback="")
        for name in skill_names
    ]
    active = [s for s in snippets if s]
    if not active:
        return base

    return base + "\n\n" + "\n\n".join(active)
