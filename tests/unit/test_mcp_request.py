"""Unit tests for webhooks/mcp_request.py — Phase 17: Intelligent MCP Agent."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from webhooks.mcp_request import (
    McpRequest,
    McpResearchResult,
    _find_in_registry,
)

API_KEY = "test-praetor-key"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture
def client(mock_hatchet, monkeypatch):
    monkeypatch.setenv("PRAETOR_API_KEY", API_KEY)
    with patch("webhooks.vikunja.register_webhook_on_startup", new=AsyncMock()):
        from webhooks.app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ---------------------------------------------------------------------------
# McpRequest validation
# ---------------------------------------------------------------------------

def test_mcp_request_valid():
    req = McpRequest(capability="query Grafana alerts via the HTTP API")
    assert req.preferred_name is None


def test_mcp_request_capability_too_short():
    with pytest.raises(Exception):
        McpRequest(capability="grafana")


def test_mcp_request_with_preferred_name():
    req = McpRequest(capability="send push notifications via ntfy service", preferred_name="mcp-ntfy")
    assert req.preferred_name == "mcp-ntfy"


def test_mcp_request_strips_whitespace():
    req = McpRequest(capability="  query Grafana alerts   ")
    assert req.capability == "query Grafana alerts"


# ---------------------------------------------------------------------------
# _find_in_registry
# ---------------------------------------------------------------------------

def test_find_in_registry_hit():
    registry = {
        "mcp-searxng": {"name": "mcp-searxng", "image": "img:latest"},
        "mcp-grafana": {"name": "mcp-grafana", "image": "img:latest"},
    }
    assert _find_in_registry(registry, "search the web with searxng") == "mcp-searxng"


def test_find_in_registry_miss():
    registry = {"mcp-grafana": {"name": "mcp-grafana", "image": "img:latest"}}
    assert _find_in_registry(registry, "send slack notifications") is None


def test_find_in_registry_empty():
    assert _find_in_registry({}, "anything") is None


def test_find_in_registry_case_insensitive():
    registry = {"mcp-searxng": {"name": "mcp-searxng", "image": "img:latest"}}
    assert _find_in_registry(registry, "SearXNG web search") == "mcp-searxng"


# ---------------------------------------------------------------------------
# POST /api/v1/mcp/request — auth
# ---------------------------------------------------------------------------

def test_request_mcp_missing_auth(client):
    resp = client.post("/api/v1/mcp/request", json={"capability": "query Grafana alerts"})
    assert resp.status_code == 401


def test_request_mcp_wrong_key(client):
    resp = client.post(
        "/api/v1/mcp/request",
        headers={"Authorization": "Bearer wrong"},
        json={"capability": "query Grafana alerts"},
    )
    assert resp.status_code == 401


def test_request_mcp_capability_too_short(client):
    resp = client.post(
        "/api/v1/mcp/request",
        headers=AUTH,
        json={"capability": "short"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/mcp/request — already registered (duplicate prevention)
# ---------------------------------------------------------------------------

def test_request_mcp_already_registered(client):
    registry = {"mcp-searxng": {"name": "mcp-searxng", "image": "img:latest", "pr_url": "https://github.com/pr/1"}}
    with patch("webhooks.mcp_request._load_registry", new=AsyncMock(return_value=registry)):
        resp = client.post(
            "/api/v1/mcp/request",
            headers=AUTH,
            json={"capability": "search the web with searxng"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "already_registered"
    assert "mcp-searxng" in data["message"]
    assert data["pr_url"] == "https://github.com/pr/1"


# ---------------------------------------------------------------------------
# POST /api/v1/mcp/request — use_existing path (confidence >= 0.85)
# ---------------------------------------------------------------------------

def _mock_register_resp(pr_url: str):
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"name": "mcp-grafana", "pr_url": pr_url, "message": "ok"}
    mock.post = AsyncMock(return_value=resp)
    return mock


def test_request_mcp_use_existing(client):
    high_confidence = McpResearchResult(
        found=True,
        confidence=0.92,
        image="ghcr.io/grafana/mcp-grafana:latest",
        name="mcp-grafana",
        notes="mcp-grafana found on Smithery with Docker image.",
    )
    pr_url = "https://github.com/amerenda/k3s-dean-gitops/pull/999"
    with (
        patch("webhooks.mcp_request._load_registry", new=AsyncMock(return_value={})),
        patch("webhooks.mcp_request._research_mcp", new=AsyncMock(return_value=high_confidence)),
        patch("webhooks.mcp_request.httpx.AsyncClient", return_value=_mock_register_resp(pr_url)),
    ):
        resp = client.post(
            "/api/v1/mcp/request",
            headers=AUTH,
            json={"capability": "query Grafana alerts from the Grafana HTTP API"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "use_existing"
    assert data["pr_url"] == pr_url
    assert data["image"] == "ghcr.io/grafana/mcp-grafana:latest"
    assert "92%" in data["message"]


def test_request_mcp_use_existing_forwards_preferred_name(client):
    """preferred_name is passed through to mcp/register as the name."""
    high_confidence = McpResearchResult(
        found=True,
        confidence=0.90,
        image="ghcr.io/org/mcp-grafana:latest",
        name="mcp-grafana",
        notes="Found on Smithery.",
    )
    pr_url = "https://github.com/amerenda/k3s-dean-gitops/pull/100"
    with (
        patch("webhooks.mcp_request._load_registry", new=AsyncMock(return_value={})),
        patch("webhooks.mcp_request._research_mcp", new=AsyncMock(return_value=high_confidence)),
        patch("webhooks.mcp_request.httpx.AsyncClient", return_value=_mock_register_resp(pr_url)),
    ):
        resp = client.post(
            "/api/v1/mcp/request",
            headers=AUTH,
            json={
                "capability": "query Grafana alerts from the Grafana HTTP API",
                "preferred_name": "my-grafana-mcp",
            },
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/v1/mcp/request — scaffold_new path (low confidence)
# ---------------------------------------------------------------------------

def test_request_mcp_scaffold_new_low_confidence(client):
    low_confidence = McpResearchResult(
        found=False,
        confidence=0.2,
        image=None,
        name=None,
        notes="No existing MCP found for this capability.",
    )
    with (
        patch("webhooks.mcp_request._load_registry", new=AsyncMock(return_value={})),
        patch("webhooks.mcp_request._research_mcp", new=AsyncMock(return_value=low_confidence)),
    ):
        resp = client.post(
            "/api/v1/mcp/request",
            headers=AUTH,
            json={"capability": "send a push notification via ntfy service"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "scaffold_new"
    assert data["task_id"] is not None
    assert "hatchet.amer.dev" in data["message"]


def test_request_mcp_scaffold_preferred_name_used(client, mock_hatchet):
    low_confidence = McpResearchResult(
        found=False, confidence=0.1, notes="Nothing found.",
    )
    with (
        patch("webhooks.mcp_request._load_registry", new=AsyncMock(return_value={})),
        patch("webhooks.mcp_request._research_mcp", new=AsyncMock(return_value=low_confidence)),
    ):
        resp = client.post(
            "/api/v1/mcp/request",
            headers=AUTH,
            json={
                "capability": "send a push notification via ntfy service",
                "preferred_name": "mcp-ntfy",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "scaffold_new"
    # scaffold event must reference the preferred name
    pushed_payload = mock_hatchet.event.push.call_args[0][1]
    assert "mcp-ntfy" in pushed_payload["task_title"]


def test_request_mcp_scaffold_derives_name_from_capability(client, mock_hatchet):
    low_confidence = McpResearchResult(
        found=False, confidence=0.0, notes="Nothing found.",
    )
    with (
        patch("webhooks.mcp_request._load_registry", new=AsyncMock(return_value={})),
        patch("webhooks.mcp_request._research_mcp", new=AsyncMock(return_value=low_confidence)),
    ):
        resp = client.post(
            "/api/v1/mcp/request",
            headers=AUTH,
            json={"capability": "send push notifications to mobile devices"},
        )
    assert resp.status_code == 200
    # name derived from first two words of capability: "mcp-send-push"
    pushed_payload = mock_hatchet.event.push.call_args[0][1]
    assert "mcp-" in pushed_payload["task_title"]


def test_request_mcp_scaffold_uses_research_name(client, mock_hatchet):
    """If research found a name but no image (confidence < threshold), use that name."""
    partial = McpResearchResult(
        found=True,
        confidence=0.70,
        image=None,
        name="mcp-ntfy",
        notes="Found mcp-ntfy but no prebuilt image.",
    )
    with (
        patch("webhooks.mcp_request._load_registry", new=AsyncMock(return_value={})),
        patch("webhooks.mcp_request._research_mcp", new=AsyncMock(return_value=partial)),
    ):
        resp = client.post(
            "/api/v1/mcp/request",
            headers=AUTH,
            json={"capability": "send push notifications via ntfy service"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "scaffold_new"
    pushed_payload = mock_hatchet.event.push.call_args[0][1]
    assert "mcp-ntfy" in pushed_payload["task_title"]


# ---------------------------------------------------------------------------
# POST /api/v1/mcp/request — exact threshold boundary
# ---------------------------------------------------------------------------

def test_request_mcp_below_threshold_scaffolds(client):
    """confidence=0.84 (just below 0.85) → scaffold_new even with image."""
    below = McpResearchResult(
        found=True,
        confidence=0.84,
        image="ghcr.io/org/mcp-grafana:latest",
        name="mcp-grafana",
        notes="Uncertain match.",
    )
    with (
        patch("webhooks.mcp_request._load_registry", new=AsyncMock(return_value={})),
        patch("webhooks.mcp_request._research_mcp", new=AsyncMock(return_value=below)),
    ):
        resp = client.post(
            "/api/v1/mcp/request",
            headers=AUTH,
            json={"capability": "query Grafana alerts from the Grafana HTTP API"},
        )
    assert resp.status_code == 200
    assert resp.json()["decision"] == "scaffold_new"


def test_request_mcp_at_threshold_uses_existing(client):
    """confidence=0.85 exactly → use_existing."""
    at_threshold = McpResearchResult(
        found=True,
        confidence=0.85,
        image="ghcr.io/org/mcp-grafana:latest",
        name="mcp-grafana",
        notes="Found on Smithery.",
    )
    pr_url = "https://github.com/amerenda/k3s-dean-gitops/pull/200"
    with (
        patch("webhooks.mcp_request._load_registry", new=AsyncMock(return_value={})),
        patch("webhooks.mcp_request._research_mcp", new=AsyncMock(return_value=at_threshold)),
        patch("webhooks.mcp_request.httpx.AsyncClient", return_value=_mock_register_resp(pr_url)),
    ):
        resp = client.post(
            "/api/v1/mcp/request",
            headers=AUTH,
            json={"capability": "query Grafana alerts from the Grafana HTTP API"},
        )
    assert resp.status_code == 200
    assert resp.json()["decision"] == "use_existing"


# ---------------------------------------------------------------------------
# POST /api/v1/mcp/request — use_existing with no name in research or request
# ---------------------------------------------------------------------------

def test_request_mcp_use_existing_no_name_returns_422(client):
    """High confidence but no name from research and no preferred_name → 422."""
    no_name = McpResearchResult(
        found=True,
        confidence=0.95,
        image="ghcr.io/org/something:latest",
        name=None,
        notes="Image found but name unclear.",
    )
    with (
        patch("webhooks.mcp_request._load_registry", new=AsyncMock(return_value={})),
        patch("webhooks.mcp_request._research_mcp", new=AsyncMock(return_value=no_name)),
    ):
        resp = client.post(
            "/api/v1/mcp/request",
            headers=AUTH,
            json={"capability": "query Grafana alerts from the Grafana HTTP API"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/mcp/request — downstream mcp/register conflict
# ---------------------------------------------------------------------------

def test_request_mcp_register_conflict_propagated(client):
    high_confidence = McpResearchResult(
        found=True,
        confidence=0.92,
        image="ghcr.io/org/mcp-grafana:latest",
        name="mcp-grafana",
        notes="Found on Smithery.",
    )
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
    mock_cm.__aexit__ = AsyncMock(return_value=None)
    conflict_resp = MagicMock()
    conflict_resp.status_code = 409
    conflict_resp.json.return_value = {"detail": "already registered"}
    mock_cm.post = AsyncMock(return_value=conflict_resp)

    with (
        patch("webhooks.mcp_request._load_registry", new=AsyncMock(return_value={})),
        patch("webhooks.mcp_request._research_mcp", new=AsyncMock(return_value=high_confidence)),
        patch("webhooks.mcp_request.httpx.AsyncClient", return_value=mock_cm),
    ):
        resp = client.post(
            "/api/v1/mcp/request",
            headers=AUTH,
            json={"capability": "query Grafana alerts from the Grafana HTTP API"},
        )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Kubernetes MCP — requires_k8s_sa causes RBAC fields in registration
# ---------------------------------------------------------------------------

def test_request_mcp_kubernetes_includes_rbac(client):
    """k8s MCP research (requires_k8s_sa=True) → registration body has SA + cluster_role."""
    k8s_result = McpResearchResult(
        found=True,
        confidence=0.95,
        image="flux159/mcp-server-kubernetes:latest",
        name="kubernetes-readonly",
        notes="mcp-server-kubernetes found on Docker Hub (flux159). Actively maintained.",
        port=3000,
        requires_k8s_sa=True,
    )
    pr_url = "https://github.com/amerenda/k3s-dean-gitops/pull/900"

    captured_body: dict = {}

    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
    mock_cm.__aexit__ = AsyncMock(return_value=None)
    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {"name": "kubernetes-readonly", "pr_url": pr_url, "message": "ok"}

    async def capture_post(url, *, json=None, headers=None):
        captured_body.update(json or {})
        return ok_resp

    mock_cm.post = capture_post

    with (
        patch("webhooks.mcp_request._load_registry", new=AsyncMock(return_value={})),
        patch("webhooks.mcp_request._research_mcp", new=AsyncMock(return_value=k8s_result)),
        patch("webhooks.mcp_request.httpx.AsyncClient", return_value=mock_cm),
    ):
        resp = client.post(
            "/api/v1/mcp/request",
            headers=AUTH,
            json={"capability": "query and manage Kubernetes cluster pods and deployments"},
        )
    assert resp.status_code == 200
    assert resp.json()["decision"] == "use_existing"
    assert captured_body["port"] == 3000
    assert captured_body["service_account_name"] == "kubernetes-readonly-sa"
    assert captured_body["cluster_role"] == "view"


def test_request_mcp_no_k8s_sa_when_not_required(client):
    """Non-k8s MCP → no service_account_name or cluster_role in registration body."""
    non_k8s_result = McpResearchResult(
        found=True,
        confidence=0.92,
        image="ghcr.io/org/mcp-grafana:latest",
        name="mcp-grafana",
        notes="Found on Smithery.",
        port=8080,
        requires_k8s_sa=False,
    )
    pr_url = "https://github.com/amerenda/k3s-dean-gitops/pull/901"

    captured_body: dict = {}

    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
    mock_cm.__aexit__ = AsyncMock(return_value=None)
    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {"name": "mcp-grafana", "pr_url": pr_url, "message": "ok"}

    async def capture_post(url, *, json=None, headers=None):
        captured_body.update(json or {})
        return ok_resp

    mock_cm.post = capture_post

    with (
        patch("webhooks.mcp_request._load_registry", new=AsyncMock(return_value={})),
        patch("webhooks.mcp_request._research_mcp", new=AsyncMock(return_value=non_k8s_result)),
        patch("webhooks.mcp_request.httpx.AsyncClient", return_value=mock_cm),
    ):
        resp = client.post(
            "/api/v1/mcp/request",
            headers=AUTH,
            json={"capability": "query Grafana alerts from the Grafana HTTP API"},
        )
    assert resp.status_code == 200
    assert captured_body.get("service_account_name") is None
    assert captured_body.get("cluster_role") is None
    assert captured_body["port"] == 8080
