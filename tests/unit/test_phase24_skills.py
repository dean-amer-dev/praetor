"""Unit tests for Phase 24: Skills System."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# common.skills tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_assemble_prompt_no_db():
    """assemble_prompt returns base unchanged when DB is unavailable."""
    from common.skills import assemble_prompt

    with patch("common.skills.get_pool", new_callable=AsyncMock, return_value=None):
        result = await assemble_prompt("coder", "BASE PROMPT")

    assert result == "BASE PROMPT"


@pytest.mark.asyncio
async def test_assemble_prompt_no_skills():
    """assemble_prompt returns base unchanged when agent has no skill assignments."""
    from common.skills import assemble_prompt

    mock_pool = AsyncMock()
    mock_pool.fetch.return_value = []

    with patch("common.skills.get_pool", new_callable=AsyncMock, return_value=mock_pool):
        result = await assemble_prompt("coder", "BASE PROMPT")

    assert result == "BASE PROMPT"


@pytest.mark.asyncio
async def test_assemble_prompt_with_skills():
    """assemble_prompt appends Langfuse skill snippets to base prompt."""
    from common.skills import assemble_prompt

    mock_pool = AsyncMock()
    mock_pool.fetch.return_value = [
        {"skill_name": "edit-pr"},
        {"skill_name": "web-research"},
    ]

    with (
        patch("common.skills.get_pool", new_callable=AsyncMock, return_value=mock_pool),
        patch("common.skills.get_system_prompt", side_effect=lambda name, fallback="": f"SNIPPET:{name}"),
    ):
        result = await assemble_prompt("coder", "BASE")

    assert result.startswith("BASE\n\n")
    assert "SNIPPET:skill-edit-pr" in result
    assert "SNIPPET:skill-web-research" in result


@pytest.mark.asyncio
async def test_assemble_prompt_empty_langfuse_snippets():
    """assemble_prompt returns base when all Langfuse skill lookups return empty."""
    from common.skills import assemble_prompt

    mock_pool = AsyncMock()
    mock_pool.fetch.return_value = [{"skill_name": "ghost-skill"}]

    with (
        patch("common.skills.get_pool", new_callable=AsyncMock, return_value=mock_pool),
        patch("common.skills.get_system_prompt", return_value=""),
    ):
        result = await assemble_prompt("coder", "BASE")

    assert result == "BASE"


@pytest.mark.asyncio
async def test_load_skill_assignments_db_error():
    """load_skill_assignments returns [] on DB error."""
    from common.skills import load_skill_assignments

    mock_pool = AsyncMock()
    mock_pool.fetch.side_effect = RuntimeError("connection lost")

    with patch("common.skills.get_pool", new_callable=AsyncMock, return_value=mock_pool):
        result = await load_skill_assignments("coder")

    assert result == []


# ---------------------------------------------------------------------------
# webhooks.skills API tests
# ---------------------------------------------------------------------------

def _make_client():
    from webhooks.app import app
    return TestClient(app)


_AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("PRAETOR_API_KEY", "test-key")


@pytest.fixture
def mock_pool():
    pool = AsyncMock()
    with patch("webhooks.skills.get_pool", new_callable=AsyncMock, return_value=pool):
        yield pool


def test_list_skills_empty(mock_pool):
    mock_pool.fetch.return_value = []
    client = _make_client()
    resp = client.get("/api/v1/skills", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_skills_returns_rows(mock_pool):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    mock_pool.fetch.return_value = [
        {"name": "edit-pr", "description": "Edit PRs", "prompt": "SNIP", "created_at": now, "updated_at": now}
    ]
    client = _make_client()
    resp = client.get("/api/v1/skills", headers=_AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "edit-pr"


def test_create_skill(mock_pool):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    mock_pool.fetchrow.return_value = {
        "name": "edit-pr",
        "description": "Edit PRs",
        "prompt": "SNIP",
        "created_at": now,
        "updated_at": now,
    }
    with patch("webhooks.skills.create_prompt", return_value=True):
        client = _make_client()
        resp = client.post(
            "/api/v1/skills",
            headers=_AUTH,
            json={"name": "edit-pr", "description": "Edit PRs", "prompt": "SNIP"},
        )
    assert resp.status_code == 201
    assert resp.json()["name"] == "edit-pr"


def test_create_skill_duplicate(mock_pool):
    mock_pool.fetchrow.side_effect = Exception("unique constraint violation")
    with patch("webhooks.skills.create_prompt", return_value=True):
        client = _make_client()
        resp = client.post(
            "/api/v1/skills",
            headers=_AUTH,
            json={"name": "edit-pr", "description": "x", "prompt": "y"},
        )
    assert resp.status_code == 409


def test_get_skill_not_found(mock_pool):
    mock_pool.fetchrow.return_value = None
    client = _make_client()
    resp = client.get("/api/v1/skills/ghost", headers=_AUTH)
    assert resp.status_code == 404


def test_delete_skill_not_found(mock_pool):
    mock_pool.execute.return_value = "DELETE 0"
    client = _make_client()
    resp = client.delete("/api/v1/skills/ghost", headers=_AUTH)
    assert resp.status_code == 404


def test_delete_skill_ok(mock_pool):
    mock_pool.execute.return_value = "DELETE 1"
    client = _make_client()
    resp = client.delete("/api/v1/skills/edit-pr", headers=_AUTH)
    assert resp.status_code == 204


def test_assign_skill_not_found(mock_pool):
    mock_pool.fetchrow.return_value = None
    client = _make_client()
    resp = client.post(
        "/api/v1/agents/coder/skills",
        headers=_AUTH,
        json={"skill_name": "ghost"},
    )
    assert resp.status_code == 404


def test_assign_skill_ok(mock_pool):
    mock_pool.fetchrow.return_value = {"name": "edit-pr"}
    mock_pool.execute.return_value = None
    mock_pool.fetch.return_value = [{"skill_name": "edit-pr"}]
    client = _make_client()
    resp = client.post(
        "/api/v1/agents/coder/skills",
        headers=_AUTH,
        json={"skill_name": "edit-pr"},
    )
    assert resp.status_code == 201
    assert "edit-pr" in resp.json()["skills"]


def test_remove_skill_assignment_not_found(mock_pool):
    mock_pool.execute.return_value = "DELETE 0"
    client = _make_client()
    resp = client.delete("/api/v1/agents/coder/skills/ghost", headers=_AUTH)
    assert resp.status_code == 404


def test_remove_skill_assignment_ok(mock_pool):
    mock_pool.execute.return_value = "DELETE 1"
    client = _make_client()
    resp = client.delete("/api/v1/agents/coder/skills/edit-pr", headers=_AUTH)
    assert resp.status_code == 204


def test_list_agents_with_skills(mock_pool):
    mock_pool.fetch.return_value = [
        {"agent_name": "coder", "skills": ["edit-pr", "web-research"]},
        {"agent_name": "reviewer", "skills": ["post-review"]},
    ]
    client = _make_client()
    resp = client.get("/api/v1/agents", headers=_AUTH)
    assert resp.status_code == 200
    agents = resp.json()
    assert len(agents) == 2
    assert agents[0]["agent_name"] == "coder"


def test_skills_auth_required():
    client = _make_client()
    resp = client.get("/api/v1/skills")
    assert resp.status_code == 401


def test_skills_db_unavailable():
    with patch("webhooks.skills.get_pool", new_callable=AsyncMock, return_value=None):
        client = _make_client()
        resp = client.get("/api/v1/skills", headers=_AUTH)
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# build_agent system_prompt override tests
# ---------------------------------------------------------------------------

def test_coder_build_agent_custom_prompt():
    """build_agent(system_prompt=...) does not raise."""
    with (
        patch.dict("os.environ", {"LITELLM_BASE_URL": "http://x", "LITELLM_API_KEY": "k"}),
        patch("agents.coder.agent.OpenAIChatModel", return_value=MagicMock()),
        patch("agents.coder.agent.OpenAIProvider", return_value=MagicMock()),
    ):
        import importlib, agents.coder.agent as m
        importlib.reload(m)
        agent = m.build_agent(system_prompt="CUSTOM")
        assert agent is not None


def test_research_build_agent_custom_prompt():
    """build_agent(system_prompt=...) does not raise."""
    with (
        patch.dict("os.environ", {"LITELLM_BASE_URL": "http://x", "LITELLM_API_KEY": "k"}),
        patch("agents.research.agent.OpenAIChatModel", return_value=MagicMock()),
        patch("agents.research.agent.OpenAIProvider", return_value=MagicMock()),
    ):
        import importlib, agents.research.agent as m
        importlib.reload(m)
        agent = m.build_agent(system_prompt="CUSTOM")
        assert agent is not None
