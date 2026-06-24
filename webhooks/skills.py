"""FastAPI router: skills CRUD + agent-skill assignment (Phase 24)."""
from __future__ import annotations

import logging
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from common.db import get_pool
from common.langfuse_tools import create_prompt, get_system_prompt

logger = logging.getLogger(__name__)
router = APIRouter()
_bearer = HTTPBearer(auto_error=False)


def _check_auth(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    expected = os.environ.get("PRAETOR_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=500, detail="PRAETOR_API_KEY not configured on server")
    token = creds.credentials if creds else ""
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


async def _require_pool():
    pool = await get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="database unavailable — PRAETOR_DB_URL not set")
    return pool


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class SkillCreate(BaseModel):
    name: str
    description: str
    prompt: str


class SkillUpdate(BaseModel):
    description: str | None = None
    prompt: str | None = None


class SkillResponse(BaseModel):
    name: str
    description: str
    prompt: str
    created_at: datetime
    updated_at: datetime


class AgentSkillAssign(BaseModel):
    skill_name: str


class AgentResponse(BaseModel):
    agent_name: str
    skills: list[str]


# ---------------------------------------------------------------------------
# Skills CRUD
# ---------------------------------------------------------------------------

@router.get("/api/v1/skills", dependencies=[Depends(_check_auth)])
async def list_skills() -> list[SkillResponse]:
    """List all skills."""
    pool = await _require_pool()
    rows = await pool.fetch("SELECT name, description, prompt, created_at, updated_at FROM praetor_skills ORDER BY name")
    return [SkillResponse(**dict(r)) for r in rows]


@router.post("/api/v1/skills", dependencies=[Depends(_check_auth)], status_code=201)
async def create_skill(body: SkillCreate) -> SkillResponse:
    """Create a skill. Also pushes the prompt text to Langfuse as skill-{name}."""
    pool = await _require_pool()
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO praetor_skills (name, description, prompt)
            VALUES ($1, $2, $3)
            RETURNING name, description, prompt, created_at, updated_at
            """,
            body.name, body.description, body.prompt,
        )
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise HTTPException(status_code=409, detail=f"skill '{body.name}' already exists")
        raise HTTPException(status_code=500, detail=str(exc))

    create_prompt(f"skill-{body.name}", body.prompt)
    return SkillResponse(**dict(row))


@router.get("/api/v1/skills/{name}", dependencies=[Depends(_check_auth)])
async def get_skill(name: str) -> SkillResponse:
    """Get a skill plus its current Langfuse version (prompt field reflects Langfuse)."""
    pool = await _require_pool()
    row = await pool.fetchrow(
        "SELECT name, description, prompt, created_at, updated_at FROM praetor_skills WHERE name = $1",
        name,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"skill '{name}' not found")
    live_prompt = get_system_prompt(f"skill-{name}", fallback=row["prompt"])
    return SkillResponse(
        name=row["name"],
        description=row["description"],
        prompt=live_prompt,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.put("/api/v1/skills/{name}", dependencies=[Depends(_check_auth)])
async def update_skill(name: str, body: SkillUpdate) -> SkillResponse:
    """Update a skill's description or prompt. Prompt update also pushes to Langfuse."""
    pool = await _require_pool()
    row = await pool.fetchrow("SELECT * FROM praetor_skills WHERE name = $1", name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"skill '{name}' not found")

    new_desc = body.description if body.description is not None else row["description"]
    new_prompt = body.prompt if body.prompt is not None else row["prompt"]

    updated = await pool.fetchrow(
        """
        UPDATE praetor_skills SET description = $2, prompt = $3, updated_at = NOW()
        WHERE name = $1
        RETURNING name, description, prompt, created_at, updated_at
        """,
        name, new_desc, new_prompt,
    )

    if body.prompt is not None:
        create_prompt(f"skill-{name}", new_prompt)

    return SkillResponse(**dict(updated))


@router.delete("/api/v1/skills/{name}", dependencies=[Depends(_check_auth)], status_code=204)
async def delete_skill(name: str) -> None:
    """Delete a skill (cascades to agent assignments)."""
    pool = await _require_pool()
    result = await pool.execute("DELETE FROM praetor_skills WHERE name = $1", name)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail=f"skill '{name}' not found")


# ---------------------------------------------------------------------------
# Agent-skill assignments
# ---------------------------------------------------------------------------

@router.get("/api/v1/agents", dependencies=[Depends(_check_auth)])
async def list_agents() -> list[AgentResponse]:
    """List all agents that have at least one skill assigned."""
    pool = await _require_pool()
    rows = await pool.fetch(
        "SELECT agent_name, array_agg(skill_name ORDER BY assigned_at) AS skills "
        "FROM praetor_agent_skills GROUP BY agent_name ORDER BY agent_name"
    )
    return [AgentResponse(agent_name=r["agent_name"], skills=r["skills"]) for r in rows]


@router.get("/api/v1/agents/{agent_name}/skills", dependencies=[Depends(_check_auth)])
async def list_agent_skills(agent_name: str) -> AgentResponse:
    """List active skills for a specific agent."""
    pool = await _require_pool()
    rows = await pool.fetch(
        "SELECT skill_name FROM praetor_agent_skills WHERE agent_name = $1 ORDER BY assigned_at",
        agent_name,
    )
    return AgentResponse(agent_name=agent_name, skills=[r["skill_name"] for r in rows])


@router.post("/api/v1/agents/{agent_name}/skills", dependencies=[Depends(_check_auth)], status_code=201)
async def assign_skill(agent_name: str, body: AgentSkillAssign) -> AgentResponse:
    """Assign a skill to an agent. Idempotent."""
    pool = await _require_pool()
    skill = await pool.fetchrow("SELECT name FROM praetor_skills WHERE name = $1", body.skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"skill '{body.skill_name}' not found")

    await pool.execute(
        "INSERT INTO praetor_agent_skills (agent_name, skill_name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        agent_name, body.skill_name,
    )
    return await list_agent_skills(agent_name)


@router.delete(
    "/api/v1/agents/{agent_name}/skills/{skill_name}",
    dependencies=[Depends(_check_auth)],
    status_code=204,
)
async def remove_skill(agent_name: str, skill_name: str) -> None:
    """Remove a skill assignment from an agent."""
    pool = await _require_pool()
    result = await pool.execute(
        "DELETE FROM praetor_agent_skills WHERE agent_name = $1 AND skill_name = $2",
        agent_name, skill_name,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail=f"assignment '{agent_name}/{skill_name}' not found")
