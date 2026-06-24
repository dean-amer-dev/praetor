"""Unit tests for Phase 22: spec layer — _parse_spec, spec routing, memory search endpoint."""
from __future__ import annotations

import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agents.coder.worker import _parse_spec


# ---------------------------------------------------------------------------
# _parse_spec
# ---------------------------------------------------------------------------

_NEW_APP_SPEC = textwrap.dedent("""\
    [task]
    type        = "new_app"
    title       = "Blog homepage"
    description = "Stateless React blog"

    [repos]
    primary = "amerenda/blog"
    gitops  = "amerenda/k3s-dean-gitops"

    [infra]
    type          = "stateless"
    k3s_namespace = "apps"
    hostname      = "blog.amer.dev"
    port          = 3000

    [app]
    runtime   = "node"
    framework = "react-vite"
    features  = [
        "Post listing page",
        "Individual post page",
        "Dark mode toggle",
    ]

    [dispatch]
    agents           = ["app_factory", "coder"]
    reviewer_enabled = true
    redispatch_cap   = 3
    request_limit    = 80

    [context]
    prior_memory = []
""")

_FIX_PR_SPEC = textwrap.dedent("""\
    [task]
    type  = "fix_pr"
    title = "Fix reviewer feedback"

    [repos]
    primary = "amerenda/my-app"

    [pr]
    number   = 42
    branch   = "praetor-coder/task-123"
    feedback = "Missing error handling"
    attempt  = 1

    [dispatch]
    redispatch_cap = 2
    request_limit  = 40
""")

_MODIFY_APP_SPEC = textwrap.dedent("""\
    [task]
    type  = "modify_app"
    title = "Add dark mode"

    [repos]
    primary = "amerenda/blog"

    [app]
    changes = ["Add dark mode toggle", "Persist setting in localStorage"]

    [dispatch]
    request_limit = 60
""")


def _wrap(toml: str) -> str:
    return f"Some text before\n```toml\n{toml}```\nSome text after"


def test_parse_spec_new_app():
    spec = _parse_spec(_wrap(_NEW_APP_SPEC))
    assert spec is not None
    assert spec["task"]["type"] == "new_app"
    assert spec["repos"]["primary"] == "amerenda/blog"
    assert spec["infra"]["port"] == 3000
    assert "Post listing page" in spec["app"]["features"]
    assert spec["dispatch"]["request_limit"] == 80


def test_parse_spec_fix_pr():
    spec = _parse_spec(_wrap(_FIX_PR_SPEC))
    assert spec is not None
    assert spec["task"]["type"] == "fix_pr"
    assert spec["pr"]["number"] == 42
    assert spec["pr"]["branch"] == "praetor-coder/task-123"
    assert spec["dispatch"]["redispatch_cap"] == 2


def test_parse_spec_modify_app():
    spec = _parse_spec(_wrap(_MODIFY_APP_SPEC))
    assert spec is not None
    assert spec["task"]["type"] == "modify_app"
    assert "Add dark mode toggle" in spec["app"]["changes"]


def test_parse_spec_no_block():
    assert _parse_spec("No TOML block here.") is None


def test_parse_spec_bad_toml():
    assert _parse_spec("```toml\n[invalid toml\n```") is None


def test_parse_spec_empty_description():
    assert _parse_spec("") is None


# ---------------------------------------------------------------------------
# POST /api/v1/spec/execute — routing logic
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    import os
    os.environ.setdefault("PRAETOR_API_KEY", "test-key")
    os.environ.setdefault("MEM0_BASE_URL", "http://mem0.test")
    os.environ.setdefault("MEM0_API_KEY", "mem0-key")
    from webhooks.app import app
    return TestClient(app, raise_server_exceptions=True)


def _auth():
    return {"Authorization": "Bearer test-key"}


def test_spec_execute_new_app_routes_to_app_factory(client):
    with (
        patch("webhooks.spec._route_new_app", new_callable=AsyncMock) as mock_new_app,
    ):
        from webhooks.spec import SpecExecuteResponse
        mock_new_app.return_value = SpecExecuteResponse(
            task_id=999, event="agent:code", hatchet_url="https://hatchet.amer.dev",
            routing="app_factory",
        )
        resp = client.post(
            "/api/v1/spec/execute",
            json={"spec_toml": _NEW_APP_SPEC},
            headers=_auth(),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["routing"] == "app_factory"
    mock_new_app.assert_called_once()


def test_spec_execute_fix_pr_routes_to_dispatch(client):
    with patch("webhooks.spec._route_dispatch", new_callable=AsyncMock) as mock_dispatch:
        from webhooks.spec import SpecExecuteResponse
        mock_dispatch.return_value = SpecExecuteResponse(
            task_id=888, event="agent:code", hatchet_url="https://hatchet.amer.dev",
            routing="dispatch",
        )
        resp = client.post(
            "/api/v1/spec/execute",
            json={"spec_toml": _FIX_PR_SPEC},
            headers=_auth(),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["routing"] == "dispatch"
    mock_dispatch.assert_called_once()


def test_spec_execute_bad_toml_returns_422(client):
    resp = client.post(
        "/api/v1/spec/execute",
        json={"spec_toml": "[invalid toml"},
        headers=_auth(),
    )
    assert resp.status_code == 422


def test_spec_execute_unknown_task_type_returns_422(client):
    bad_spec = "[task]\ntype = \"unknown\"\n"
    resp = client.post(
        "/api/v1/spec/execute",
        json={"spec_toml": bad_spec},
        headers=_auth(),
    )
    assert resp.status_code == 422


def test_spec_execute_requires_auth(client):
    resp = client.post("/api/v1/spec/execute", json={"spec_toml": _FIX_PR_SPEC})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/memory/search
# ---------------------------------------------------------------------------

def test_memory_search_returns_results(client):
    with patch("webhooks.spec.search_memory", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = ["amerenda/blog was deployed at blog.amer.dev"]
        resp = client.post(
            "/api/v1/memory/search",
            json={"query": "blog"},
            headers=_auth(),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == ["amerenda/blog was deployed at blog.amer.dev"]
    mock_search.assert_called_once_with("blog", "planner-global")


def test_memory_search_requires_auth(client):
    resp = client.post("/api/v1/memory/search", json={"query": "blog"})
    assert resp.status_code == 401
