"""Unit tests for webhooks/mcp_factory.py."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from webhooks.mcp_factory import (
    McpRegistration,
    _NGINX_PROXY_PORT,
    _SECRET_TIERS,
    _argocd_application_yaml,
    _build_manifests,
    _cluster_role_binding_yaml,
    _deployment_yaml,
    _externalsecret_yaml,
    _litellm_mcp_entry,
    _migrate_entry,
    _nginx_configmap_yaml,
    _service_account_yaml,
    _service_yaml,
    _upsert_litellm_config,
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
    return {"name": "test-mcp", "image": "example/test-mcp:latest", "skip_pre_review": True, **kwargs}


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
        assert "replicas: 1" in yaml

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
        assert "env:" not in yaml

    def test_env_vars_rendered_as_plain_values(self):
        reg = McpRegistration(name="k8s", image="img:1", env_vars={"HOST": "0.0.0.0", "FLAG": "true"})
        yaml = _deployment_yaml(reg)
        assert "name: HOST" in yaml
        assert "value: '0.0.0.0'" in yaml
        assert "name: FLAG" in yaml
        assert "secretKeyRef" not in yaml

    def test_env_vars_and_secrets_combined(self):
        reg = McpRegistration(
            name="combo", image="img:1",
            env_vars={"HOST": "0.0.0.0"},
            env_secrets={"API_KEY": "bws-secret"},
        )
        yaml = _deployment_yaml(reg)
        assert "name: HOST" in yaml
        assert "name: API_KEY" in yaml
        assert "secretKeyRef" in yaml

    def test_service_account_name_injected(self):
        reg = McpRegistration(name="k8s", image="img:1", service_account_name="k8s-sa")
        yaml = _deployment_yaml(reg)
        assert "serviceAccountName: k8s-sa" in yaml

    def test_no_service_account_when_not_set(self):
        reg = McpRegistration(name="nosvc", image="img:1")
        yaml = _deployment_yaml(reg)
        assert "serviceAccountName" not in yaml

    def test_custom_health_path_uses_httpget(self):
        reg = McpRegistration(name="mcp", image="img:1", health_path="/healthz")
        yaml = _deployment_yaml(reg)
        assert "httpGet:" in yaml
        assert "path: /healthz" in yaml
        assert "tcpSocket:" not in yaml

    def test_default_probe_is_tcp_socket(self):
        reg = McpRegistration(name="mcp", image="img:1")
        yaml = _deployment_yaml(reg)
        assert "tcpSocket:" in yaml
        assert "httpGet:" not in yaml
        assert "path:" not in yaml


# ---------------------------------------------------------------------------
# nginx config syntax helpers (no extra dependencies)
# ---------------------------------------------------------------------------

def _extract_nginx_conf(configmap_yaml: str) -> str:
    """Pull the embedded nginx config out of the ConfigMap block scalar."""
    marker = "  default.conf: |\n"
    idx = configmap_yaml.index(marker) + len(marker)
    lines = []
    for line in configmap_yaml[idx:].splitlines():
        if not line.startswith("    "):
            break
        lines.append(line[4:])
    return "\n".join(lines)


def _directive_lines_missing_semicolons(nginx_conf: str) -> list[str]:
    """Return lines that look like nginx directives but lack a trailing semicolon."""
    bad = []
    for raw in nginx_conf.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped in ("{", "}"):
            continue
        if stripped.endswith("{"):
            continue
        if not stripped.endswith(";"):
            bad.append(raw)
    return bad


# ---------------------------------------------------------------------------
# host_rewrite manifest tests
# ---------------------------------------------------------------------------

class TestHostRewriteManifests:
    def _hr_reg(self, port: int = 8000) -> McpRegistration:
        return McpRegistration(name="kube", image="flux159/mcp-server-kubernetes:latest",
                               port=port, host_rewrite=True)

    def test_deployment_includes_nginx_sidecar(self):
        yaml = _deployment_yaml(self._hr_reg())
        assert "name: host-proxy" in yaml
        assert "image: nginx:alpine" in yaml
        assert f"containerPort: {_NGINX_PROXY_PORT}" in yaml

    def test_deployment_mounts_nginx_configmap_volume(self):
        yaml = _deployment_yaml(self._hr_reg())
        assert "nginx-conf" in yaml
        assert "kube-nginx-proxy" in yaml
        assert "/etc/nginx/conf.d" in yaml

    def test_deployment_without_host_rewrite_has_no_sidecar(self):
        reg = McpRegistration(name="kube", image="img:1")
        yaml = _deployment_yaml(reg)
        assert "host-proxy" not in yaml
        assert "nginx" not in yaml
        assert "nginx-conf" not in yaml

    def test_service_routes_to_nginx_port_when_host_rewrite(self):
        yaml = _service_yaml(self._hr_reg())
        assert f"targetPort: {_NGINX_PROXY_PORT}" in yaml

    def test_service_routes_to_app_port_without_host_rewrite(self):
        reg = McpRegistration(name="foo", image="img:1", port=9000)
        yaml = _service_yaml(reg)
        assert "targetPort: 9000" in yaml
        assert f"targetPort: {_NGINX_PROXY_PORT}" not in yaml

    def test_build_manifests_includes_nginx_configmap(self):
        manifests = _build_manifests(self._hr_reg())
        assert "nginx-proxy-configmap.yaml" in manifests

    def test_build_manifests_without_host_rewrite_excludes_nginx_configmap(self):
        reg = McpRegistration(name="foo", image="img:1")
        manifests = _build_manifests(reg)
        assert "nginx-proxy-configmap.yaml" not in manifests


class TestNginxConfigMap:
    def _reg(self, port: int = 8000) -> McpRegistration:
        return McpRegistration(name="kube", image="img:1", port=port, host_rewrite=True)

    def test_structure(self):
        yaml = _nginx_configmap_yaml(self._reg())
        assert "kind: ConfigMap" in yaml
        assert "name: kube-nginx-proxy" in yaml
        assert "namespace: mcp-kube" in yaml
        assert "default.conf:" in yaml

    def test_proxies_to_correct_app_port(self):
        nginx_conf = _extract_nginx_conf(_nginx_configmap_yaml(self._reg(port=9123)))
        assert "proxy_pass http://127.0.0.1:9123" in nginx_conf

    def test_listens_on_proxy_port(self):
        nginx_conf = _extract_nginx_conf(_nginx_configmap_yaml(self._reg()))
        assert f"listen {_NGINX_PROXY_PORT}" in nginx_conf

    def test_rewrites_host_to_localhost(self):
        nginx_conf = _extract_nginx_conf(_nginx_configmap_yaml(self._reg()))
        assert "proxy_set_header Host localhost" in nginx_conf

    def test_all_directives_have_semicolons(self):
        nginx_conf = _extract_nginx_conf(_nginx_configmap_yaml(self._reg()))
        bad = _directive_lines_missing_semicolons(nginx_conf)
        assert bad == [], f"nginx directives missing semicolons:\n" + "\n".join(bad)


# ---------------------------------------------------------------------------


class TestServiceAccountYaml:
    def test_basic_structure(self):
        reg = McpRegistration(name="k8s-ro", image="img:1", service_account_name="k8s-ro-sa")
        yaml = _service_account_yaml(reg)
        assert "kind: ServiceAccount" in yaml
        assert "name: k8s-ro-sa" in yaml
        assert "namespace: mcp-k8s-ro" in yaml


class TestClusterRoleBindingYaml:
    def test_basic_structure(self):
        reg = McpRegistration(
            name="k8s-ro", image="img:1",
            service_account_name="k8s-ro-sa",
            cluster_role="view",
        )
        yaml = _cluster_role_binding_yaml(reg)
        assert "kind: ClusterRoleBinding" in yaml
        assert "name: k8s-ro-view-binding" in yaml
        assert "name: view" in yaml
        assert "name: k8s-ro-sa" in yaml
        assert "namespace: mcp-k8s-ro" in yaml


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


class TestUpsertLiteLLMConfig:
    # Realistic configmap snippet with the mcp_servers / litellm_settings structure.
    _BASE = (
        "    mcp_servers:\n"
        "      web:\n"
        '        url: "http://web-server/mcp"\n'
        '        transport: "http"\n'
        "\n"
        "    litellm_settings:\n"
        "      callbacks:\n"
        "        - some_hook\n"
    )

    def _reg(self, name: str = "foo", port: int = 8000) -> McpRegistration:
        return McpRegistration(name=name, image="img:1", port=port)

    def test_insert_before_litellm_settings(self):
        content, changed = _upsert_litellm_config(self._BASE, self._reg("foo"))
        assert changed
        assert "      foo:\n" in content
        # Must be inside mcp_servers, i.e., before litellm_settings
        foo_pos = content.index("      foo:")
        settings_pos = content.index("    litellm_settings:")
        assert foo_pos < settings_pos

    def test_insert_does_not_go_inside_litellm_settings(self):
        content, _ = _upsert_litellm_config(self._BASE, self._reg("newmcp"))
        # litellm_settings block should only contain 'callbacks'
        settings_idx = content.index("    litellm_settings:")
        after_settings = content[settings_idx:]
        assert "newmcp" not in after_settings

    def test_idempotent_replace_existing_entry(self):
        # Add foo once
        content, _ = _upsert_litellm_config(self._BASE, self._reg("foo", port=8000))
        # Re-register with updated port — should replace, not duplicate
        content2, changed2 = _upsert_litellm_config(content, self._reg("foo", port=9999))
        assert changed2
        assert content2.count("      foo:") == 1
        assert "9999" in content2

    def test_unchanged_when_entry_identical(self):
        content, _ = _upsert_litellm_config(self._BASE, self._reg("foo", port=8000))
        content2, changed = _upsert_litellm_config(content, self._reg("foo", port=8000))
        assert not changed

    def test_fallback_appends_when_no_litellm_settings(self):
        bare = "    mcp_servers:\n      web:\n        url: x\n"
        content, changed = _upsert_litellm_config(bare, self._reg("foo"))
        assert changed
        assert "      foo:" in content


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

_MOCK_CM = (
    "    mcp_servers:\n"
    "      web:\n"
    '        url: "http://web/mcp"\n'
    '        transport: "http"\n'
    "\n"
    "    litellm_settings:\n"
    "      callbacks:\n"
    "        - hook\n"
)


class TestRegisterEndpoint:
    def test_auth_required(self, client):
        resp = client.post("/api/v1/mcp/register", json=_minimal_reg())
        assert resp.status_code in (401, 403)

    def test_wrong_key_rejected(self, client):
        resp = client.post("/api/v1/mcp/register", json=_minimal_reg(), headers=_auth("bad"))
        assert resp.status_code == 401

    def test_duplicate_without_skip_manifests_returns_409(self, client):
        existing = {"test-mcp": {"name": "test-mcp", "image": "img:1", "port": 8000, "transport": "http"}}
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value=existing)),
            patch("webhooks.mcp_factory._save_registry", new=AsyncMock()),
        ):
            resp = client.post("/api/v1/mcp/register", json=_minimal_reg(), headers=_auth())
        assert resp.status_code == 409
        assert "already registered" in resp.json()["detail"]

    def test_duplicate_with_skip_manifests_succeeds(self, client):
        existing = {"test-mcp": {"name": "test-mcp", "image": "img:1", "port": 8000, "transport": "http"}}
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value=existing)),
            patch("webhooks.mcp_factory._save_registry", new=AsyncMock()),
            patch("webhooks.mcp_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.mcp_factory._smoke_test_mcp_endpoint", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_main_sha", new=AsyncMock(return_value="abc123")),
            patch("webhooks.mcp_factory._create_branch", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_file", new=AsyncMock(return_value=(_MOCK_CM, "sha1"))),
            patch("webhooks.mcp_factory._update_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_or_create_pr", new=AsyncMock(return_value="https://github.com/amerenda/k3s-dean-gitops/pull/99")),
        ):
            resp = client.post(
                "/api/v1/mcp/register",
                json=_minimal_reg(skip_manifests=True),
                headers=_auth(),
            )
        assert resp.status_code == 200
        assert "pull/99" in resp.json()["pr_url"]

    def test_skip_manifests_creates_no_files(self, client):
        create_file_mock = AsyncMock()
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})),
            patch("webhooks.mcp_factory._save_registry", new=AsyncMock()),
            patch("webhooks.mcp_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.mcp_factory._smoke_test_mcp_endpoint", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_main_sha", new=AsyncMock(return_value="abc123")),
            patch("webhooks.mcp_factory._create_branch", new=AsyncMock()),
            patch("webhooks.mcp_factory._create_file", new=create_file_mock),
            patch("webhooks.mcp_factory._get_file", new=AsyncMock(return_value=(_MOCK_CM, "sha1"))),
            patch("webhooks.mcp_factory._update_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_or_create_pr", new=AsyncMock(return_value="https://github.com/pr/1")),
        ):
            resp = client.post(
                "/api/v1/mcp/register",
                json=_minimal_reg(skip_manifests=True),
                headers=_auth(),
            )
        assert resp.status_code == 200
        create_file_mock.assert_not_awaited()

    def test_successful_registration_returns_pr_url(self, client):
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})),
            patch("webhooks.mcp_factory._save_registry", new=AsyncMock()),
            patch("webhooks.mcp_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.mcp_factory._get_main_sha", new=AsyncMock(return_value="abc123")),
            patch("webhooks.mcp_factory._create_branch", new=AsyncMock()),
            patch("webhooks.mcp_factory._create_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_file", new=AsyncMock(return_value=(_MOCK_CM, "sha1"))),
            patch("webhooks.mcp_factory._update_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_or_create_pr", new=AsyncMock(return_value="https://github.com/amerenda/k3s-dean-gitops/pull/99")),
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
            patch("webhooks.mcp_factory._get_or_create_pr", new=AsyncMock(return_value="https://github.com/pr/1")),
        ):
            resp = client.post(
                "/api/v1/mcp/register",
                json=_minimal_reg(env_secrets={"MY_KEY": "bws-secret"}),
                headers=_auth(),
            )
        assert resp.status_code == 200
        paths = [call.args[2] for call in create_file_mock.await_args_list]
        assert any("externalsecret" in p for p in paths)

    def test_rbac_files_created_when_cluster_role_set(self, client):
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
            patch("webhooks.mcp_factory._get_or_create_pr", new=AsyncMock(return_value="https://github.com/pr/1")),
        ):
            resp = client.post(
                "/api/v1/mcp/register",
                json=_minimal_reg(service_account_name="test-mcp-sa", cluster_role="view"),
                headers=_auth(),
            )
        assert resp.status_code == 200
        paths = [call.args[2] for call in create_file_mock.await_args_list]
        assert any("serviceaccount" in p for p in paths)
        assert any("clusterrolebinding" in p for p in paths)

    def test_no_rbac_files_without_cluster_role(self, client):
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
            patch("webhooks.mcp_factory._get_or_create_pr", new=AsyncMock(return_value="https://github.com/pr/1")),
        ):
            resp = client.post("/api/v1/mcp/register", json=_minimal_reg(), headers=_auth())
        assert resp.status_code == 200
        paths = [call.args[2] for call in create_file_mock.await_args_list]
        assert not any("serviceaccount" in p for p in paths)
        assert not any("clusterrolebinding" in p for p in paths)


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
            "searxng": {
                "current": {"name": "searxng", "image": "img:1", "port": 8000, "transport": "http", "status": "pending"},
                "history": [],
            },
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
            patch("webhooks.mcp_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.mcp_factory._create_branch", new=AsyncMock()),
            patch("webhooks.mcp_factory._list_gitops_dir", new=AsyncMock(return_value=[])),
            patch("webhooks.mcp_factory._delete_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_file", new=AsyncMock(return_value=("", "sha1"))),
            patch("webhooks.mcp_factory._update_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_or_create_pr", new=AsyncMock(return_value="https://github.com/pr/1")),
        ):
            resp = client.delete("/api/v1/mcp/old-mcp", headers=_auth())
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "old-mcp"
        saved = save_mock.call_args[0][0]
        assert "old-mcp" not in saved


# ---------------------------------------------------------------------------
# Pre-review loop — unit tests for the pure functions
# ---------------------------------------------------------------------------

class TestPreReviewLoopUnit:
    async def test_approved_immediately_returns_no_warning(self):
        from webhooks.mcp_factory import _pre_review_loop, McpRegistration
        reg = McpRegistration(name="foo", image="img:1")
        manifests = {"deployment.yaml": "original content"}
        with patch("webhooks.mcp_factory._call_review_llm", new=AsyncMock(return_value=(True, [], {}))):
            result, warning = await _pre_review_loop(manifests, reg)
        assert warning is None
        assert result == manifests

    async def test_fixes_applied_between_iterations(self):
        from webhooks.mcp_factory import _pre_review_loop, McpRegistration
        reg = McpRegistration(name="foo", image="img:1")
        manifests = {"deployment.yaml": "original"}
        with patch(
            "webhooks.mcp_factory._call_review_llm",
            new=AsyncMock(side_effect=[
                (False, ["probe type mismatch"], {"deployment.yaml": "fixed"}),
                (True, [], {}),
            ]),
        ):
            result, warning = await _pre_review_loop(manifests, reg)
        assert warning is None
        assert result["deployment.yaml"] == "fixed"

    async def test_max_iterations_yields_warning(self):
        from webhooks.mcp_factory import _pre_review_loop, _MAX_REVIEW_ITERATIONS, McpRegistration
        reg = McpRegistration(name="foo", image="img:1")
        manifests = {"deployment.yaml": "content"}
        with patch(
            "webhooks.mcp_factory._call_review_llm",
            new=AsyncMock(return_value=(False, ["persistent probe issue"], {})),
        ):
            result, warning = await _pre_review_loop(manifests, reg)
        assert warning is not None
        assert "persistent probe issue" in warning
        assert str(_MAX_REVIEW_ITERATIONS) in warning

    async def test_only_calls_llm_once_when_approved_first_iteration(self):
        from webhooks.mcp_factory import _pre_review_loop, McpRegistration
        reg = McpRegistration(name="foo", image="img:1")
        mock = AsyncMock(return_value=(True, [], {}))
        with patch("webhooks.mcp_factory._call_review_llm", new=mock):
            await _pre_review_loop({"deployment.yaml": "x"}, reg)
        assert mock.call_count == 1


# ---------------------------------------------------------------------------
# Pre-review loop — integration via route handler
# ---------------------------------------------------------------------------

class TestPreReviewViaRoute:
    _GH_PATCHES = {
        "webhooks.mcp_factory._load_registry": lambda: AsyncMock(return_value={}),
        "webhooks.mcp_factory._save_registry": lambda: AsyncMock(),
        "webhooks.mcp_factory.get_installation_token": lambda: MagicMock(return_value="gh-token"),
        "webhooks.mcp_factory._get_main_sha": lambda: AsyncMock(return_value="abc123"),
        "webhooks.mcp_factory._create_branch": lambda: AsyncMock(),
        "webhooks.mcp_factory._create_file": lambda: AsyncMock(),
        "webhooks.mcp_factory._get_file": lambda: AsyncMock(return_value=("content\n", "sha1")),
        "webhooks.mcp_factory._update_file": lambda: AsyncMock(),
    }

    def _gh_ctx(self, extra: dict | None = None):
        patches = {k: patch(k, new=v()) for k, v in self._GH_PATCHES.items()}
        if extra:
            patches.update(extra)
        return patches

    def test_skip_pre_review_bypasses_llm(self, client):
        review_mock = AsyncMock()
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})),
            patch("webhooks.mcp_factory._save_registry", new=AsyncMock()),
            patch("webhooks.mcp_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.mcp_factory._get_main_sha", new=AsyncMock(return_value="abc123")),
            patch("webhooks.mcp_factory._create_branch", new=AsyncMock()),
            patch("webhooks.mcp_factory._create_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_file", new=AsyncMock(return_value=("content\n", "sha1"))),
            patch("webhooks.mcp_factory._update_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_or_create_pr", new=AsyncMock(return_value="https://github.com/pr/1")),
            patch("webhooks.mcp_factory._call_review_llm", new=review_mock),
        ):
            resp = client.post("/api/v1/mcp/register", json=_minimal_reg(), headers=_auth())
        assert resp.status_code == 200
        review_mock.assert_not_called()

    def test_approved_review_no_warning_in_pr_body(self, client):
        create_pr_mock = AsyncMock(return_value="https://github.com/pr/1")
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})),
            patch("webhooks.mcp_factory._save_registry", new=AsyncMock()),
            patch("webhooks.mcp_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.mcp_factory._get_main_sha", new=AsyncMock(return_value="abc123")),
            patch("webhooks.mcp_factory._create_branch", new=AsyncMock()),
            patch("webhooks.mcp_factory._create_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_file", new=AsyncMock(return_value=("content\n", "sha1"))),
            patch("webhooks.mcp_factory._update_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_or_create_pr", new=create_pr_mock),
            patch("webhooks.mcp_factory._call_review_llm", new=AsyncMock(return_value=(True, [], {}))),
        ):
            resp = client.post(
                "/api/v1/mcp/register",
                json={**_minimal_reg(), "skip_pre_review": False},
                headers=_auth(),
            )
        assert resp.status_code == 200
        pr_body = create_pr_mock.call_args.args[3]
        assert "warning" not in pr_body.lower()
        assert "pre-review" not in pr_body.lower()

    def test_exhausted_review_adds_warning_to_pr_body(self, client):
        create_pr_mock = AsyncMock(return_value="https://github.com/pr/1")
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})),
            patch("webhooks.mcp_factory._save_registry", new=AsyncMock()),
            patch("webhooks.mcp_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.mcp_factory._get_main_sha", new=AsyncMock(return_value="abc123")),
            patch("webhooks.mcp_factory._create_branch", new=AsyncMock()),
            patch("webhooks.mcp_factory._create_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_file", new=AsyncMock(return_value=("content\n", "sha1"))),
            patch("webhooks.mcp_factory._update_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_or_create_pr", new=create_pr_mock),
            patch(
                "webhooks.mcp_factory._call_review_llm",
                new=AsyncMock(return_value=(False, ["probe type wrong"], {})),
            ),
        ):
            resp = client.post(
                "/api/v1/mcp/register",
                json={**_minimal_reg(), "skip_pre_review": False},
                headers=_auth(),
            )
        assert resp.status_code == 200
        pr_body = create_pr_mock.call_args.args[3]
        assert "pre-review warning" in pr_body.lower()
        assert "probe type wrong" in pr_body

    def test_review_fixes_applied_to_pushed_manifests(self, client):
        create_file_mock = AsyncMock()
        fixed_deployment = "apiVersion: apps/v1\nkind: Deployment\n# fixed by reviewer"
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})),
            patch("webhooks.mcp_factory._save_registry", new=AsyncMock()),
            patch("webhooks.mcp_factory.get_installation_token", return_value="gh-token"),
            patch("webhooks.mcp_factory._get_main_sha", new=AsyncMock(return_value="abc123")),
            patch("webhooks.mcp_factory._create_branch", new=AsyncMock()),
            patch("webhooks.mcp_factory._create_file", new=create_file_mock),
            patch("webhooks.mcp_factory._get_file", new=AsyncMock(return_value=("content\n", "sha1"))),
            patch("webhooks.mcp_factory._update_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_or_create_pr", new=AsyncMock(return_value="https://github.com/pr/1")),
            patch(
                "webhooks.mcp_factory._call_review_llm",
                new=AsyncMock(side_effect=[
                    (False, ["probe mismatch"], {"deployment.yaml": fixed_deployment}),
                    (True, [], {}),
                ]),
            ),
        ):
            resp = client.post(
                "/api/v1/mcp/register",
                json={**_minimal_reg(), "skip_pre_review": False},
                headers=_auth(),
            )
        assert resp.status_code == 200
        # args: (gh_client, token, path, content, message, branch)
        pushed = {call.args[2]: call.args[3] for call in create_file_mock.await_args_list}
        assert pushed.get("apps/mcp/test-mcp/deployment.yaml") == fixed_deployment


# ---------------------------------------------------------------------------
# Version history
# ---------------------------------------------------------------------------

class TestVersionHistory:
    def test_migrate_entry_promotes_flat_to_versioned(self):
        flat = {"name": "foo", "image": "img:1", "port": 8000, "transport": "http", "status": "pending"}
        result = _migrate_entry(flat)
        assert result["current"] == flat
        assert result["history"] == []

    def test_migrate_entry_passthrough_when_already_versioned(self):
        versioned = {"current": {"name": "foo"}, "history": []}
        assert _migrate_entry(versioned) is versioned

    def test_history_endpoint_returns_entry(self, client):
        registry = {
            "foo": {
                "current": {"name": "foo", "image": "img:2", "port": 8000,
                            "transport": "http", "status": "pending"},
                "history": [{"name": "foo", "image": "img:1", "port": 8000,
                              "transport": "http", "status": "pending",
                              "registered_at": "2026-01-01T00:00:00+00:00",
                              "deregistered_at": "2026-06-01T00:00:00+00:00"}],
            }
        }
        with patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value=registry)):
            resp = client.get("/api/v1/mcp/foo/history", headers=_auth())
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "foo"
        assert data["current"]["image"] == "img:2"
        assert len(data["history"]) == 1
        assert data["history"][0]["image"] == "img:1"

    def test_history_endpoint_not_found(self, client):
        with patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})):
            resp = client.get("/api/v1/mcp/nonexistent/history", headers=_auth())
        assert resp.status_code == 404

    def test_rollback_with_no_history_returns_409(self, client):
        registry = {
            "foo": {"current": {"name": "foo", "image": "img:2", "port": 8000,
                                "transport": "http", "status": "pending"}, "history": []}
        }
        with patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value=registry)):
            resp = client.post("/api/v1/mcp/foo/rollback", headers=_auth())
        assert resp.status_code == 409

    def test_rollback_not_found_returns_404(self, client):
        with patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})):
            resp = client.post("/api/v1/mcp/nonexistent/rollback", headers=_auth())
        assert resp.status_code == 404

    def test_latest_image_warning_in_pr_body(self, client):
        pr_mock = AsyncMock(return_value="https://github.com/pr/1")
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})),
            patch("webhooks.mcp_factory._save_registry", new=AsyncMock()),
            patch("webhooks.mcp_factory.get_installation_token", return_value="tok"),
            patch("webhooks.mcp_factory._get_main_sha", new=AsyncMock(return_value="abc")),
            patch("webhooks.mcp_factory._create_branch", new=AsyncMock()),
            patch("webhooks.mcp_factory._create_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_file", new=AsyncMock(return_value=("content\n", "sha1"))),
            patch("webhooks.mcp_factory._update_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_or_create_pr", new=pr_mock),
        ):
            resp = client.post(
                "/api/v1/mcp/register",
                json={"name": "foo", "image": "foo:latest", "skip_pre_review": True},
                headers=_auth(),
            )
        assert resp.status_code == 200
        body = pr_mock.call_args.args[3]
        assert ":latest" in body
        assert "pinning" in body.lower()

    def test_pinned_image_no_latest_warning(self, client):
        pr_mock = AsyncMock(return_value="https://github.com/pr/1")
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})),
            patch("webhooks.mcp_factory._save_registry", new=AsyncMock()),
            patch("webhooks.mcp_factory.get_installation_token", return_value="tok"),
            patch("webhooks.mcp_factory._get_main_sha", new=AsyncMock(return_value="abc")),
            patch("webhooks.mcp_factory._create_branch", new=AsyncMock()),
            patch("webhooks.mcp_factory._create_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_file", new=AsyncMock(return_value=("content\n", "sha1"))),
            patch("webhooks.mcp_factory._update_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_or_create_pr", new=pr_mock),
        ):
            resp = client.post(
                "/api/v1/mcp/register",
                json={"name": "foo", "image": "foo:sha-abc1234", "skip_pre_review": True},
                headers=_auth(),
            )
        assert resp.status_code == 200
        body = pr_mock.call_args.args[3]
        assert "pinning" not in body.lower()


# ---------------------------------------------------------------------------
# Secret tiers
# ---------------------------------------------------------------------------

class TestSecretTiers:
    def test_known_tier_base_expands_correctly(self):
        tier = _SECRET_TIERS["tier-base"]
        assert "hatchet-worker-api-token" in tier
        assert "litellm-master-key" in tier

    def test_tier_standard_is_superset_of_base(self):
        base_keys = set(_SECRET_TIERS["tier-base"])
        std_keys = set(_SECRET_TIERS["tier-standard"])
        assert base_keys.issubset(std_keys)

    def test_tier_github_is_superset_of_standard(self):
        std_keys = set(_SECRET_TIERS["tier-standard"])
        gh_keys = set(_SECRET_TIERS["tier-github"])
        assert std_keys.issubset(gh_keys)

    def test_unknown_tier_returns_422(self, client):
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})),
            patch("webhooks.mcp_factory._save_registry", new=AsyncMock()),
            patch("webhooks.mcp_factory.get_installation_token", return_value="tok"),
        ):
            resp = client.post(
                "/api/v1/mcp/register",
                json={"name": "foo", "image": "img:1", "skip_pre_review": True,
                      "secret_tiers": ["tier-does-not-exist"]},
                headers=_auth(),
            )
        assert resp.status_code == 422
        assert "tier-does-not-exist" in resp.json()["detail"]

    def test_tier_standard_generates_externalsecret(self, client):
        create_file_mock = AsyncMock()
        with (
            patch("webhooks.mcp_factory._load_registry", new=AsyncMock(return_value={})),
            patch("webhooks.mcp_factory._save_registry", new=AsyncMock()),
            patch("webhooks.mcp_factory.get_installation_token", return_value="tok"),
            patch("webhooks.mcp_factory._get_main_sha", new=AsyncMock(return_value="abc")),
            patch("webhooks.mcp_factory._create_branch", new=AsyncMock()),
            patch("webhooks.mcp_factory._create_file", new=create_file_mock),
            patch("webhooks.mcp_factory._get_file", new=AsyncMock(return_value=("content\n", "sha1"))),
            patch("webhooks.mcp_factory._update_file", new=AsyncMock()),
            patch("webhooks.mcp_factory._get_or_create_pr", new=AsyncMock(return_value="https://github.com/pr/1")),
        ):
            resp = client.post(
                "/api/v1/mcp/register",
                json={"name": "myagent", "image": "img:1", "skip_pre_review": True,
                      "secret_tiers": ["tier-standard"]},
                headers=_auth(),
            )
        assert resp.status_code == 200
        paths = [call.args[2] for call in create_file_mock.await_args_list]
        assert any("externalsecret" in p for p in paths), f"no externalsecret in pushed files: {paths}"
