"""Unit tests for webhooks/mcp_factory.py."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from webhooks.mcp_factory import (
    McpRegistration,
    _argocd_application_yaml,
    _deployment_yaml,
    _externalsecret_yaml,
    _litellm_mcp_entry,
    _service_yaml,
)

API_KEY = "test-praetor-key"


@pytest.fixture
def client(mock_hatchet, monkeypatch):
    monkeypatch.setenv("PRAETOR_API_KEY", API_KEY)
    with patch("webhooks.vikunja.register_webhook_on_startup", new=AsyncMock()):
        from webhooks.app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


def _auth(key: str = API_KEY) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _minimal_reg(**kwargs) -> dict:
    return {"name": "test-mcp", "image": "example/test-mcp:latest", **kwargs}


# ---------------------------------------------------------------------------
# Manifest generators
# ---------------------------------------------------------------------------

class TestDeploymentYaml:
    def test_basic_structure(self):
        reg = McpRegistration(name="foo", image="example/foo:latest", port=8000)
        yaml = _deployment_yaml(reg)
        assert "name: foo-server" in yaml
        assert "namespace: mcp-foo" in yaml
        assert "image: example/foo:latest" in yaml
        assert "containerPort: 8000" in yaml
        assert "cpu: 10m" in yaml

    def test_env_secrets_injected(self):
        reg = McpRegistration(
            name="bar", image="example/bar:latest",
            env_secrets={"MY_KEY": "my-bws-secret"}
        )
        yaml = _deployment_yaml(reg)
        assert "name: MY_KEY" in yaml
        assert "name: bar-secrets" in yaml
        assert "key: my_key" in yaml

    def test_args_included(self):
        reg = McpRegistration(name="baz", image="img:1", args=["--read-only"])
        yaml = _deployment_yaml(reg)
        assert "--read-only" in yaml

    def test_no_env_block_when_empty(self):
        reg = McpRegistration(name="clean", image="img:1")
        yaml = _deployment_yaml(reg)
        assert "secretKeyRef" not in yaml


class TestServiceYaml:
    def test_basic_structure(self):
        reg = McpRegistration(name="mysvc", image="img:1", port=9000)
        yaml = _service_yaml(reg)
        assert "name: mysvc-server" in yaml
        assert "namespace: mcp-mysvc" in yaml
        assert "port: 9000" in yaml
        assert "targetPort: 9000" in yaml


class TestExternalSecretYaml:
    def test_secret_entries(self):
        reg = McpRegistration(
            name="sec",
            image="img:1",
            env_secrets={"API_KEY": "my-api-key-bws", "DB_PASS": "db-password-bws"},
        )
        yaml = _externalsecret_yaml(reg)
        assert "name: sec-secrets" in yaml
        assert "namespace: mcp-sec" in yaml
        assert "key: my-api-key-bws" in yaml
        assert "key: db-password-bws" in yaml
        assert "secretKey: api_key" in yaml
        assert "secretKey: db_pass" in yaml


class TestArgoCDApplicationYaml:
    def test_argocd_structure(self):
        reg = McpRegistration(name="mymcp", image="img:1")
        yaml = _argocd_application_yaml(reg)
        assert "name: app-mymcp-server" in yaml
        assert "path: apps/mcp/mymcp" in yaml
        assert "namespace: mcp-mymcp" in yaml
        assert 'sync-wave: "5"' in yaml
        assert "CreateNamespace=true" in yaml


class TestLiteLLMMcpEntry:
    def test_entry_format(self):
        reg = McpRegistration(name="kube", image="img:1", port=8080, transport="http")
        entry = _litellm_mcp_entry(reg)
        assert "kube:" in entry
        assert "kube-server.mcp-kube.svc.cluster.local:8080/mcp" in entry
        assert 'transport: "http"' in entry


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

class TestRegisterEndpoint:
    def test_auth_required(self, client):
        resp = client.post("/api/v1/mcp/register", json=_minimal_reg())
        assert resp.status_code in (401, 403)

    def test_wrong_key_rejected(self, client):
        resp = client.post("/api/v1/mcp/register", json=_minimal_reg(), headers=_auth("bad"))
        assert resp.status_code == 401

    def test_duplicate_returns_409(self, client):
        existing = {"test-mcp": {"name": "test-mcp", "image": "img:1", "port": 8000, "transport": "http"}}
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value=existing)),
            patch("webhooks.mcp_factory._save_registry", new=AsyncMock()),
        ):
            resp = client.post("/api/v1/mcp/register", json=_minimal_reg(), headers=_auth())
        assert resp.status_code == 409
        assert "already registered" in resp.json()["detail"]

    def test_successful_registration_returns_pr_url(self, client):
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})),
            patch("webhooks.mcp_factory._save_registry", new=AsyncMock()),
            patch("webhooks.mcp_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.mcp_factory._get_main_sha", new=AsyncMock(return_value="abc123")),
            patch("webhooks.mcp_factory._create_branch", new=AsyncMock()),
            patch("webhooks.mcp_factory._create_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_file", new=AsyncMock(return_value=("content\n", "sha1"))),
            patch("webhooks.mcp_factory._update_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._create_pr", new=AsyncMock(return_value="https://github.com/amerenda/k3s-dean-gitops/pull/99")),
        ):
            resp = client.post("/api/v1/mcp/register", json=_minimal_reg(), headers=_auth())
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-mcp"
        assert "pull/99" in data["pr_url"]
        assert "PR created" in data["message"]

    def test_externalsecret_created_when_env_secrets_present(self, client):
        create_file_mock = AsyncMock()
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})),
            patch("webhooks.mcp_factory._save_registry", new=AsyncMock()),
            patch("webhooks.mcp_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.mcp_factory._get_main_sha", new=AsyncMock(return_value="abc123")),
            patch("webhooks.mcp_factory._create_branch", new=AsyncMock()),
            patch("webhooks.mcp_factory._create_file", new=create_file_mock),
            patch("webhooks.mcp_factory._get_file", new=AsyncMock(return_value=("content\n", "sha1"))),
            patch("webhooks.mcp_factory._update_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._create_pr", new=AsyncMock(return_value="https://github.com/pr/1")),
        ):
            resp = client.post(
                "/api/v1/mcp/register",
                json=_minimal_reg(env_secrets={"MY_KEY": "bws-secret"}),
                headers=_auth(),
            )
        assert resp.status_code == 200
        paths = [call.args[2] for call in create_file_mock.await_args_list]
        assert any("externalsecret" in p for p in paths)


class TestListEndpoint:
    def test_auth_required(self, client):
        resp = client.get("/api/v1/mcp")
        assert resp.status_code in (401, 403)

    def test_empty_registry(self, client):
        with patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})):
            resp = client.get("/api/v1/mcp", headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["mcps"] == []

    def test_returns_registered_mcps(self, client):
        registry = {
            "searxng": {"name": "searxng", "image": "img:1", "port": 8000, "transport": "http", "status": "pending"},
        }
        with patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value=registry)):
            resp = client.get("/api/v1/mcp", headers=_auth())
        assert resp.status_code == 200
        mcps = resp.json()["mcps"]
        assert len(mcps) == 1
        assert mcps[0]["name"] == "searxng"


class TestDeleteEndpoint:
    def test_auth_required(self, client):
        resp = client.delete("/api/v1/mcp/foo")
        assert resp.status_code in (401, 403)

    def test_not_found_returns_404(self, client):
        with patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})):
            resp = client.delete("/api/v1/mcp/nonexistent", headers=_auth())
        assert resp.status_code == 404

    def test_successful_delete(self, client):
        registry = {
            "old-mcp": {"name": "old-mcp", "image": "img:1", "port": 8000, "transport": "http", "status": "pending"},
        }
        save_mock = AsyncMock()
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value=registry)),
            patch("webhooks.mcp_factory._save_registry", new=save_mock),
        ):
            resp = client.delete("/api/v1/mcp/old-mcp", headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "old-mcp"
        saved = save_mock.call_args[0][0]
        assert "old-mcp" not in saved
