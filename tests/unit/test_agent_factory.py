"""Unit tests for webhooks/agent_factory.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from webhooks.agent_factory import (
    _agent_py,
    _argocd_app_yaml,
    _deployment_yaml,
    _dockerfile,
    _externalsecret_yaml,
    _scaled_object_yaml,
    _worker_py,
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


def _req(**kwargs) -> dict:
    return {
        "name": "test-agent",
        "description": "A test agent",
        "event": "agent:test",
        **kwargs,
    }


def _all_mocks_happy():
    """Return a context manager stack with all external calls mocked for a happy path."""
    return [
        patch("webhooks.agent_factory._load_agent_registry", new=AsyncMock(return_value={})),
        patch("webhooks.agent_factory._save_agent_registry", new=AsyncMock()),
        patch("webhooks.agent_factory.get_installation_token", return_value="gh-token"),
        patch("webhooks.agent_factory._get_main_sha", new=AsyncMock(return_value="abc1234567")),
        patch("webhooks.agent_factory._create_branch", new=AsyncMock()),
        patch("webhooks.agent_factory._create_file", new=AsyncMock()),
        patch("webhooks.agent_factory._get_file", new=AsyncMock(return_value=("existing root-app content", "file-sha-abc"))),
        patch("webhooks.agent_factory._update_file", new=AsyncMock()),
        patch("webhooks.agent_factory._create_pr", new=AsyncMock(return_value="https://github.com/amerenda/praetor/pull/42")),
        patch("webhooks.agent_factory._merge_pr", new=AsyncMock(return_value="deadbeef1234567")),
        patch("webhooks.agent_factory._wait_for_pr_ci", new=AsyncMock(return_value=True)),
        patch("webhooks.agent_factory._wait_for_ci_run_complete", new=AsyncMock(return_value=True)),
        patch("webhooks.agent_factory._wait_for_pod", new=AsyncMock(return_value="ready")),
        patch("webhooks.agent_factory._smoke_test", new=AsyncMock(return_value="passed")),
        patch("webhooks.agent_factory.create_prompt", return_value=True),
    ]


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------

class TestCreateAgent:
    def test_create_agent_happy_path(self, client):
        patches = _all_mocks_happy()
        ctx_managers = [p.__enter__() for p in patches]
        try:
            resp = client.post("/api/v1/agent/create", json=_req(), headers=_auth())
        finally:
            for p in reversed(patches):
                p.__exit__(None, None, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["agent"] == "test-agent"
        assert data["event"] == "agent:test"
        assert data["pod_status"] == "ready"
        assert data["smoke_test"] == "passed"
        assert data["scaffold_pr_url"] is not None
        assert data["manifest_pr_url"] is not None

    def test_create_agent_idempotent(self, client):
        existing = {
            "test-agent": {
                "name": "test-agent",
                "event": "agent:test",
                "scaffold_pr_url": "https://github.com/amerenda/praetor/pull/1",
                "manifest_pr_url": "https://github.com/amerenda/k3s-dean-gitops/pull/2",
                "status": "running",
            }
        }
        create_branch_mock = AsyncMock()
        with (
            patch("webhooks.agent_factory._load_agent_registry", new=AsyncMock(return_value=existing)),
            patch("webhooks.agent_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.agent_factory._create_branch", new=create_branch_mock),
        ):
            resp = client.post("/api/v1/agent/create", json=_req(), headers=_auth())

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert "already exists" in data["message"]
        create_branch_mock.assert_not_called()

    def test_create_agent_invalid_name_spaces(self, client):
        resp = client.post("/api/v1/agent/create", json=_req(name="my agent"), headers=_auth())
        assert resp.status_code == 422

    def test_create_agent_invalid_name_too_short(self, client):
        resp = client.post("/api/v1/agent/create", json=_req(name="ab"), headers=_auth())
        assert resp.status_code == 422

    def test_create_agent_invalid_event_uppercase(self, client):
        resp = client.post("/api/v1/agent/create", json=_req(event="AGENT:FOO"), headers=_auth())
        assert resp.status_code == 422

    def test_create_agent_scaffold_github_fails(self, client):
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 502
        mock_resp.text = "Bad Gateway"
        err = httpx.HTTPStatusError("502", request=MagicMock(), response=mock_resp)

        with (
            patch("webhooks.agent_factory._load_agent_registry", new=AsyncMock(return_value={})),
            patch("webhooks.agent_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.agent_factory._get_main_sha", new=AsyncMock(return_value="abc123")),
            patch("webhooks.agent_factory._create_branch", new=AsyncMock(side_effect=err)),
        ):
            resp = client.post("/api/v1/agent/create", json=_req(), headers=_auth())

        assert resp.status_code == 502

    def test_create_agent_ci_timeout_returns_partial(self, client):
        with (
            patch("webhooks.agent_factory._load_agent_registry", new=AsyncMock(return_value={})),
            patch("webhooks.agent_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.agent_factory._get_main_sha", new=AsyncMock(return_value="abc123")),
            patch("webhooks.agent_factory._create_branch", new=AsyncMock()),
            patch("webhooks.agent_factory._create_file", new=AsyncMock()),
            patch("webhooks.agent_factory._create_pr", new=AsyncMock(return_value="https://github.com/amerenda/praetor/pull/5")),
            patch("webhooks.agent_factory._wait_for_pr_ci", new=AsyncMock(return_value=False)),
        ):
            resp = client.post("/api/v1/agent/create", json=_req(), headers=_auth())

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "partial"
        assert "timed out" in data["message"].lower()

    def test_create_agent_ci_checks_403_proceeds_to_running(self, client):
        """Factory succeeds when _wait_for_pr_ci returns True (403 fallback path)."""
        with (
            patch("webhooks.agent_factory._load_agent_registry", new=AsyncMock(return_value={})),
            patch("webhooks.agent_factory._save_agent_registry", new=AsyncMock()),
            patch("webhooks.agent_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.agent_factory._get_main_sha", new=AsyncMock(return_value="abc1234567")),
            patch("webhooks.agent_factory._create_branch", new=AsyncMock()),
            patch("webhooks.agent_factory._create_file", new=AsyncMock()),
            patch("webhooks.agent_factory._get_file", new=AsyncMock(return_value=("root content", "sha-abc"))),
            patch("webhooks.agent_factory._update_file", new=AsyncMock()),
            patch("webhooks.agent_factory._create_pr", new=AsyncMock(return_value="https://github.com/amerenda/praetor/pull/42")),
            patch("webhooks.agent_factory._merge_pr", new=AsyncMock(return_value="deadbeef1234567")),
            patch("webhooks.agent_factory._wait_for_pr_ci", new=AsyncMock(return_value=True)),
            patch("webhooks.agent_factory._wait_for_ci_run_complete", new=AsyncMock(return_value=True)),
            patch("webhooks.agent_factory._wait_for_pod", new=AsyncMock(return_value="ready")),
            patch("webhooks.agent_factory._smoke_test", new=AsyncMock(return_value="passed")),
            patch("webhooks.agent_factory.create_prompt", return_value=True),
        ):
            resp = client.post("/api/v1/agent/create", json=_req(), headers=_auth())

        assert resp.status_code == 200
        assert resp.json()["status"] == "running"

    def test_create_agent_merge_fails_returns_partial(self, client):
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 405
        mock_resp.text = "Method Not Allowed"
        err = httpx.HTTPStatusError("405", request=MagicMock(), response=mock_resp)

        with (
            patch("webhooks.agent_factory._load_agent_registry", new=AsyncMock(return_value={})),
            patch("webhooks.agent_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.agent_factory._get_main_sha", new=AsyncMock(return_value="abc123")),
            patch("webhooks.agent_factory._create_branch", new=AsyncMock()),
            patch("webhooks.agent_factory._create_file", new=AsyncMock()),
            patch("webhooks.agent_factory._create_pr", new=AsyncMock(return_value="https://github.com/amerenda/praetor/pull/5")),
            patch("webhooks.agent_factory._wait_for_pr_ci", new=AsyncMock(return_value=True)),
            patch("webhooks.agent_factory._merge_pr", new=AsyncMock(side_effect=err)),
        ):
            resp = client.post("/api/v1/agent/create", json=_req(), headers=_auth())

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "partial"
        assert "merge failed" in data["message"].lower()

    def test_create_agent_pod_not_ready(self, client):
        patches = _all_mocks_happy()
        # Override pod status
        patches[12] = patch("webhooks.agent_factory._wait_for_pod", new=AsyncMock(return_value="not_ready"))
        ctx_managers = [p.__enter__() for p in patches]
        try:
            resp = client.post("/api/v1/agent/create", json=_req(), headers=_auth())
        finally:
            for p in reversed(patches):
                p.__exit__(None, None, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["pod_status"] == "not_ready"
        assert data["status"] == "running"

    def test_create_agent_langfuse_fail_nonfatal(self, client):
        patches = _all_mocks_happy()
        # Override create_prompt to fail
        patches[14] = patch("webhooks.agent_factory.create_prompt", return_value=False)
        ctx_managers = [p.__enter__() for p in patches]
        try:
            resp = client.post("/api/v1/agent/create", json=_req(), headers=_auth())
        finally:
            for p in reversed(patches):
                p.__exit__(None, None, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["langfuse_prompt"] is None
        assert data["status"] == "running"

    def test_create_agent_smoke_fail_nonfatal(self, client):
        patches = _all_mocks_happy()
        # Override smoke test to fail
        patches[13] = patch("webhooks.agent_factory._smoke_test", new=AsyncMock(return_value="failed"))
        ctx_managers = [p.__enter__() for p in patches]
        try:
            resp = client.post("/api/v1/agent/create", json=_req(), headers=_auth())
        finally:
            for p in reversed(patches):
                p.__exit__(None, None, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["smoke_test"] == "failed"
        assert data["status"] == "running"

    def test_create_agent_registry_save_fail_nonfatal(self, client):
        patches = _all_mocks_happy()
        # Override save to raise
        patches[1] = patch("webhooks.agent_factory._save_agent_registry", new=AsyncMock(side_effect=Exception("k8s down")))
        ctx_managers = [p.__enter__() for p in patches]
        try:
            resp = client.post("/api/v1/agent/create", json=_req(), headers=_auth())
        finally:
            for p in reversed(patches):
                p.__exit__(None, None, None)

        assert resp.status_code == 200
        assert resp.json()["status"] == "running"

    def test_get_agent_found(self, client):
        existing = {"foo-agent": {"name": "foo-agent", "event": "agent:foo", "status": "running"}}
        with patch("webhooks.agent_factory._load_agent_registry", new=AsyncMock(return_value=existing)):
            resp = client.get("/api/v1/agent/foo-agent", headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["name"] == "foo-agent"

    def test_get_agent_not_found(self, client):
        with patch("webhooks.agent_factory._load_agent_registry", new=AsyncMock(return_value={})):
            resp = client.get("/api/v1/agent/ghost", headers=_auth())
        assert resp.status_code == 404

    def test_auth_missing_token(self, client):
        resp = client.post("/api/v1/agent/create", json=_req())
        assert resp.status_code == 401

    def test_auth_wrong_token(self, client):
        resp = client.post("/api/v1/agent/create", json=_req(), headers=_auth("wrong-key"))
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Scaffold file content tests
# ---------------------------------------------------------------------------

class TestAgentPy:
    def test_agent_py_includes_search_memory(self):
        out = _agent_py("foo", "does foo things", ["search_memory"])
        assert "search_memory" in out

    def test_agent_py_includes_add_memory(self):
        out = _agent_py("foo", "does foo things", ["add_memory"])
        assert "add_memory" in out

    def test_agent_py_no_tools_minimal(self):
        out = _agent_py("foo", "does foo things", [])
        assert "search_memory" not in out
        assert "add_memory" not in out
        assert "memory_tools" not in out

    def test_agent_py_both_memory_tools_single_import(self):
        out = _agent_py("foo", "desc", ["search_memory", "add_memory"])
        assert "search_memory" in out
        assert "add_memory" in out
        # Should use a single import line for both
        assert out.count("from common.memory_tools import") == 1


class TestWorkerPy:
    def test_worker_py_correct_event(self):
        out = _worker_py("foo", "agent:foo")
        assert '"agent:foo"' in out

    def test_worker_py_phase21_pattern(self):
        out = _worker_py("bar", "agent:bar")
        assert "search_memory" in out
        assert "add_memory" in out

    def test_worker_py_class_name_from_hyphenated(self):
        out = _worker_py("grafana-monitor", "agent:grafana-monitor")
        assert "GrafanaMonitorInput" in out


class TestDockerfile:
    def test_dockerfile_cmd_correct(self):
        out = _dockerfile("grafana-monitor")
        assert "agents.grafana_monitor.worker" in out

    def test_dockerfile_single_underscore_conversion(self):
        out = _dockerfile("my-cool-agent")
        assert "agents.my_cool_agent.worker" in out

    def test_dockerfile_copy_uses_underscore_dest(self):
        out = _dockerfile("my-agent")
        assert "COPY agents/my-agent/ agents/my_agent/" in out


class TestDeploymentYaml:
    def test_deployment_yaml_uses_sha_tag(self):
        out = _deployment_yaml("foo", "sha-abc1234")
        assert "sha-abc1234" in out

    def test_deployment_yaml_secret_name(self):
        out = _deployment_yaml("foo", "sha-abc1234")
        assert "praetor-foo-secrets" in out

    def test_deployment_yaml_image_name(self):
        out = _deployment_yaml("my-agent", "sha-deadbee")
        assert "amerenda/praetor-my-agent:sha-deadbee" in out

    def test_deployment_yaml_app_label(self):
        out = _deployment_yaml("foo", "sha-abc1234")
        assert "app: praetor-foo-worker" in out

    def test_deployment_yaml_node_selector(self):
        out = _deployment_yaml("foo", "sha-abc1234")
        assert "kubernetes.io/arch: amd64" in out


class TestExternalSecretYaml:
    def test_externalsecret_base_keys(self):
        out = _externalsecret_yaml("foo")
        assert "hatchet-token" in out
        assert "hatchet-worker-api-token" in out
        assert "mem0-api-key" in out
        assert "mem0-admin-api-key" in out
        assert "litellm-api-key" in out
        assert "litellm-master-key" in out
        assert "vikunja-token" in out
        assert "vikjuna-api-key-full-access" in out
        assert "langfuse-public-key" in out
        assert "langfuse-secret-key" in out

    def test_externalsecret_coder_creds(self):
        out = _externalsecret_yaml("foo", include_coder_creds=True)
        assert "coder-app-id" in out
        assert "github-amerenda-coder-app-id" in out
        assert "coder-private-key" in out
        assert "coder-installation-id" in out

    def test_externalsecret_no_coder_creds_by_default(self):
        out = _externalsecret_yaml("foo")
        assert "coder-app-id" not in out
        assert "github-amerenda-coder-app-id" not in out

    def test_externalsecret_secret_name(self):
        out = _externalsecret_yaml("my-agent")
        assert "praetor-my-agent-secrets" in out

    def test_externalsecret_namespace(self):
        out = _externalsecret_yaml("foo")
        assert "namespace: praetor" in out


class TestArgoCDAppYaml:
    def test_argocd_app_name(self):
        out = _argocd_app_yaml("my-agent")
        assert "name: app-praetor-my-agent-worker" in out

    def test_argocd_app_path(self):
        out = _argocd_app_yaml("my-agent")
        assert "path: apps/praetor/my-agent-worker" in out

    def test_argocd_app_namespace(self):
        out = _argocd_app_yaml("my-agent")
        assert "namespace: praetor" in out

    def test_argocd_app_project(self):
        out = _argocd_app_yaml("my-agent")
        assert "project: application" in out

    def test_argocd_app_automated_sync(self):
        out = _argocd_app_yaml("my-agent")
        assert "automated:" in out
        assert "prune: true" in out
        assert "selfHeal: true" in out


class TestScaffoldPrFiles:
    def test_scaffold_pr_files_correct_paths(self, client):
        create_file_mock = AsyncMock()
        with (
            patch("webhooks.agent_factory._load_agent_registry", new=AsyncMock(return_value={})),
            patch("webhooks.agent_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.agent_factory._get_main_sha", new=AsyncMock(return_value="abc123")),
            patch("webhooks.agent_factory._create_branch", new=AsyncMock()),
            patch("webhooks.agent_factory._create_file", new=create_file_mock),
            patch("webhooks.agent_factory._create_pr", new=AsyncMock(return_value="https://github.com/amerenda/praetor/pull/5")),
            patch("webhooks.agent_factory._wait_for_pr_ci", new=AsyncMock(return_value=False)),
        ):
            client.post("/api/v1/agent/create", json=_req(name="foo-agent"), headers=_auth())

        paths = [call.args[3] for call in create_file_mock.await_args_list]
        assert "agents/foo-agent/__init__.py" in paths
        assert "agents/foo-agent/agent.py" in paths
        assert "agents/foo-agent/worker.py" in paths
        assert "agents/foo-agent/Dockerfile" in paths


class TestScaledObjectYaml:
    def test_scaled_object_yaml_contains_task_name(self):
        out = _scaled_object_yaml("my-agent", "my-agent")
        assert "my-agent.queued.total" in out
        assert "task-stats?taskNames=my-agent" in out

    def test_scaled_object_yaml_kind(self):
        out = _scaled_object_yaml("test-agent", "test-agent")
        assert "kind: ScaledObject" in out
        assert "apiVersion: keda.sh/v1alpha1" in out

    def test_scaled_object_scale_target_ref(self):
        out = _scaled_object_yaml("my-agent", "my-agent")
        assert "name: praetor-my-agent-worker" in out

    def test_scaled_object_replica_counts(self):
        out = _scaled_object_yaml("test-agent", "test-agent")
        assert "minReplicaCount: 0" in out
        assert "maxReplicaCount: 3" in out

    def test_scaled_object_cooldown_and_polling(self):
        out = _scaled_object_yaml("test-agent", "test-agent")
        assert "cooldownPeriod: 120" in out
        assert "pollingInterval: 15" in out

    def test_scaled_object_trigger_type(self):
        out = _scaled_object_yaml("test-agent", "test-agent")
        assert "type: metrics-api" in out

    def test_scaled_object_auth_ref(self):
        out = _scaled_object_yaml("test-agent", "test-agent")
        assert "name: hatchet-api-auth" in out
        assert "kind: ClusterTriggerAuthentication" in out

    def test_scaled_object_value_location(self):
        out = _scaled_object_yaml("my-agent", "my-agent")
        assert 'valueLocation: "my-agent.queued.total"' in out

    def test_scaled_object_auth_mode(self):
        out = _scaled_object_yaml("test-agent", "test-agent")
        assert "authMode: "bearer"" in out

    def test_scaled_object_namespace(self):
        out = _scaled_object_yaml("test-agent", "test-agent")
        assert "namespace: praetor" in out

