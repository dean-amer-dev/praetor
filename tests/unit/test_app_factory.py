"""Unit tests for webhooks/app_factory.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from webhooks.app_factory import AppPlan, _build_coder_description

API_KEY = "test-praetor-key"


@pytest.fixture
def client(mock_hatchet, monkeypatch):
    monkeypatch.setenv("PRAETOR_API_KEY", API_KEY)
    with patch("webhooks.vikunja.register_webhook_on_startup", new=AsyncMock()):
        from webhooks.app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


AUTH = {"Authorization": f"Bearer {API_KEY}"}


# ---------------------------------------------------------------------------
# AppPlan validation
# ---------------------------------------------------------------------------

def test_appplan_valid():
    plan = AppPlan(name="my-app", description="A test app")
    assert plan.name == "my-app"
    assert plan.stateless is True
    assert plan.port == 8000


def test_appplan_uppercase_rejected():
    with pytest.raises(Exception):
        AppPlan(name="MyApp", description="bad")


def test_appplan_underscore_rejected():
    with pytest.raises(Exception):
        AppPlan(name="my_app", description="bad")


def test_appplan_stateful_rejected():
    with pytest.raises(Exception):
        AppPlan(name="my-app", description="bad", stateless=False)


def test_appplan_defaults():
    plan = AppPlan(name="my-app", description="desc")
    assert plan.domain is None
    assert plan.has_database is False
    assert plan.env_secrets == {}
    assert plan.port == 8000


def test_appplan_custom_fields():
    plan = AppPlan(
        name="cool-svc",
        description="Does stuff",
        domain="cool.example.com",
        port=9000,
        has_database=True,
        env_secrets={"API_KEY": "cool-svc-api-key"},
    )
    assert plan.port == 9000
    assert plan.domain == "cool.example.com"
    assert plan.has_database is True
    assert plan.env_secrets == {"API_KEY": "cool-svc-api-key"}


# ---------------------------------------------------------------------------
# Coder description builder
# ---------------------------------------------------------------------------

def test_coder_description_contains_repo():
    plan = AppPlan(name="my-app", description="Does X")
    desc = _build_coder_description(plan)
    assert "Repo: amerenda/my-app" in desc


def test_coder_description_uat_url():
    plan = AppPlan(name="my-app", description="Does X")
    desc = _build_coder_description(plan)
    assert "https://my-app-uat.amer.dev" in desc


def test_coder_description_default_prod_url():
    plan = AppPlan(name="my-app", description="Does X")
    desc = _build_coder_description(plan)
    assert "https://my-app.amer.dev" in desc


def test_coder_description_custom_domain():
    plan = AppPlan(name="my-app", description="Does X", domain="app.example.com")
    desc = _build_coder_description(plan)
    assert "https://app.example.com" in desc


def test_coder_description_env_secrets():
    plan = AppPlan(
        name="my-app",
        description="Does X",
        env_secrets={"API_KEY": "my-api-key", "DB_URL": "my-db-url"},
    )
    desc = _build_coder_description(plan)
    assert "API_KEY" in desc
    assert "my-api-key" in desc
    assert "DB_URL" in desc


def test_coder_description_branch():
    plan = AppPlan(name="my-app", description="Does X")
    desc = _build_coder_description(plan)
    assert "amerenda-coder/initial-implementation" in desc


def test_coder_description_app_name_replacement_instruction():
    plan = AppPlan(name="my-app", description="Does X")
    desc = _build_coder_description(plan)
    assert "APP_NAME" in desc
    assert '"my-app"' in desc


def test_coder_description_no_secrets_section_when_empty():
    plan = AppPlan(name="my-app", description="Does X")
    desc = _build_coder_description(plan)
    assert "Environment secrets" not in desc


# ---------------------------------------------------------------------------
# POST /api/v1/app/create — auth
# ---------------------------------------------------------------------------

def test_create_app_missing_auth(client):
    resp = client.post("/api/v1/app/create", json={"name": "my-app", "description": "test"})
    assert resp.status_code == 401


def test_create_app_wrong_key(client):
    resp = client.post(
        "/api/v1/app/create",
        headers={"Authorization": "Bearer wrong-key"},
        json={"name": "my-app", "description": "test"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/app/create — input validation
# ---------------------------------------------------------------------------

def test_create_app_invalid_name_uppercase(client):
    resp = client.post(
        "/api/v1/app/create",
        headers=AUTH,
        json={"name": "MyApp", "description": "test"},
    )
    assert resp.status_code == 422


def test_create_app_stateful_rejected(client):
    resp = client.post(
        "/api/v1/app/create",
        headers=AUTH,
        json={"name": "my-app", "description": "test", "stateless": False},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/app/create — success
# ---------------------------------------------------------------------------

def _mock_github_client(html_url: str, status_code: int = 201):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = {"html_url": html_url}
    mock_resp.text = f'{{"html_url": "{html_url}"}}'
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
    mock_cm.__aexit__ = AsyncMock(return_value=None)
    mock_cm.post = AsyncMock(return_value=mock_resp)
    return mock_cm


def test_create_app_success(client):
    mock_cm = _mock_github_client("https://github.com/amerenda/my-app")
    with (
        patch("webhooks.app_factory.get_installation_token", return_value="gh-token"),
        patch("webhooks.app_factory.httpx.AsyncClient", return_value=mock_cm),
        patch("webhooks.app_factory._provision_and_dispatch", new=AsyncMock()),
    ):
        resp = client.post(
            "/api/v1/app/create",
            headers=AUTH,
            json={"name": "my-app", "description": "A simple test app"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["repo_url"] == "https://github.com/amerenda/my-app"
    assert isinstance(data["task_id"], int)
    assert "my-app" in data["message"]
    assert "hatchet.amer.dev" in data["message"]


def test_create_app_includes_uat_url_in_message(client):
    mock_cm = _mock_github_client("https://github.com/amerenda/svc-x")
    with (
        patch("webhooks.app_factory.get_installation_token", return_value="gh-token"),
        patch("webhooks.app_factory.httpx.AsyncClient", return_value=mock_cm),
        patch("webhooks.app_factory._provision_and_dispatch", new=AsyncMock()),
    ):
        resp = client.post(
            "/api/v1/app/create",
            headers=AUTH,
            json={"name": "svc-x", "description": "test service"},
        )

    assert resp.status_code == 200
    assert "svc-x-uat.amer.dev" in resp.json()["message"]


# ---------------------------------------------------------------------------
# POST /api/v1/app/create — repo already exists
# ---------------------------------------------------------------------------

def test_create_app_repo_conflict(client):
    mock_resp = MagicMock()
    mock_resp.status_code = 422
    mock_resp.json.return_value = {"message": "Repository already exists on this account"}
    mock_resp.text = '{"message": "Repository already exists on this account"}'
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
    mock_cm.__aexit__ = AsyncMock(return_value=None)
    mock_cm.post = AsyncMock(return_value=mock_resp)

    with (
        patch("webhooks.app_factory.get_installation_token", return_value="gh-token"),
        patch("webhooks.app_factory.httpx.AsyncClient", return_value=mock_cm),
    ):
        resp = client.post(
            "/api/v1/app/create",
            headers=AUTH,
            json={"name": "my-app", "description": "test"},
        )

    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# POST /api/v1/app/create — GitHub API failure
# ---------------------------------------------------------------------------

def test_create_app_github_error(client):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "internal server error"
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
    mock_cm.__aexit__ = AsyncMock(return_value=None)
    mock_cm.post = AsyncMock(return_value=mock_resp)

    with (
        patch("webhooks.app_factory.get_installation_token", return_value="gh-token"),
        patch("webhooks.app_factory.httpx.AsyncClient", return_value=mock_cm),
    ):
        resp = client.post(
            "/api/v1/app/create",
            headers=AUTH,
            json={"name": "my-app", "description": "test"},
        )

    assert resp.status_code == 502
