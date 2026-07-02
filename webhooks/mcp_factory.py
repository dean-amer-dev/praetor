"""FastAPI router: self-service MCP factory — register, list, and delete MCPs."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from common.github_app import get_installation_token
from common.langfuse_tools import get_system_prompt

logger = logging.getLogger(__name__)
router = APIRouter()
_bearer = HTTPBearer(auto_error=False)

GITOPS_REPO = "amerenda/k3s-dean-gitops"
GITHUB_API = "https://api.github.com"
REGISTRY_CM = "mcp-registry"
REGISTRY_NS = "praetor"
K8S_API = "https://kubernetes.default.svc"
_K8S_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_K8S_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

_LITELLM_BASE = os.environ.get("LITELLM_BASE_URL", "http://litellm.praetor.svc.cluster.local/v1")
_LITELLM_KEY = os.environ.get("LITELLM_API_KEY", "")
_MAX_REVIEW_ITERATIONS = 3
_NGINX_PROXY_PORT = 8080

_PRE_REVIEW_SYSTEM_FALLBACK = """\
You are a Kubernetes YAML reviewer for MCP server deployments in a GitOps pipeline. \
Review the provided manifests for correctness before they are pushed to the gitops repo.

Check for these common issues:
1. Probe type: if health_path is None (no /health endpoint), probes MUST use tcpSocket, not httpGet
2. Env completeness: required env vars are present (e.g. HOST=0.0.0.0 for servers that default to \
   loopback binding — without it the container is unreachable)
3. Service port matches container port
4. ServiceAccount correctly referenced in the Deployment if RBAC manifests are present
5. Image registry correctness (e.g. flux159/mcp-server-kubernetes is on Docker Hub, not ghcr.io)
6. Container port matches the actual listening port for the image

Return ONLY a JSON object with these exact fields:
{
  "approved": <true if all manifests are correct, false if any issues found>,
  "issues": ["<description of each issue>"],
  "fixes": {
    "<filename>": "<complete corrected yaml content>"
  }
}

If approved, set "issues": [] and "fixes": {}.
Only include files in "fixes" that need changes. Preserve all valid yaml fields.
"""


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _check_auth(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    expected = os.environ.get("PRAETOR_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=500, detail="PRAETOR_API_KEY not configured on server")
    token = creds.credentials if creds else ""
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

_SECRET_TIERS: dict[str, dict[str, str]] = {
    "tier-base": {
        "hatchet-worker-api-token": "hatchet-token",
        "litellm-master-key": "litellm-api-key",
        "praetor-db-password": "db-password",
    },
    "tier-standard": {
        "hatchet-worker-api-token": "hatchet-token",
        "litellm-master-key": "litellm-api-key",
        "praetor-db-password": "db-password",
        "mem0-admin-api-key": "mem0-api-key",
        "langfuse-public-key": "langfuse-public-key",
        "langfuse-secret-key": "langfuse-secret-key",
        "vikjuna-api-key-full-access": "vikunja-token",
    },
    "tier-github": {
        "hatchet-worker-api-token": "hatchet-token",
        "litellm-master-key": "litellm-api-key",
        "praetor-db-password": "db-password",
        "mem0-admin-api-key": "mem0-api-key",
        "langfuse-public-key": "langfuse-public-key",
        "langfuse-secret-key": "langfuse-secret-key",
        "vikjuna-api-key-full-access": "vikunja-token",
        "github-amerenda-coder-app-id": "coder-app-id",
        "github-amerenda-coder-private-key": "coder-private-key",
        "github-amerenda-coder-installation-id": "coder-installation-id",
    },
}


class McpRegistration(BaseModel):
    name: str
    image: str
    port: int = 8000
    transport: str = "http"
    env_secrets: dict[str, str] = {}
    env_vars: dict[str, str] = {}           # non-secret env vars (e.g. HOST, feature flags)
    args: list[str] = []
    service_account_name: str | None = None  # mounts this SA in the pod
    cluster_role: str | None = None          # creates SA + ClusterRoleBinding when set
    health_path: str | None = None           # if set, use httpGet probe; otherwise tcpSocket
    skip_pre_review: bool = False            # bypass inline LLM review loop
    skip_manifests: bool = False             # skip k8s manifest + ArgoCD steps; only upsert LiteLLM entry
    host_rewrite: bool = False               # when True, inject nginx sidecar to rewrite Host header
    secret_tiers: list[str] = []            # e.g. ["tier-standard"] expands to standard Praetor secrets


class McpStatusEntry(BaseModel):
    name: str
    image: str
    port: int
    transport: str
    pr_url: str | None = None
    status: str = "pending"
    service_account_name: str | None = None
    cluster_role: str | None = None
    registered_at: str | None = None


class McpHistoryEntry(BaseModel):
    name: str
    image: str
    port: int
    transport: str
    pr_url: str | None = None
    status: str = "pending"
    service_account_name: str | None = None
    cluster_role: str | None = None
    registered_at: str | None = None
    deregistered_at: str | None = None


class McpHistoryResponse(BaseModel):
    name: str
    current: McpStatusEntry
    history: list[McpHistoryEntry]


class McpRegisterResponse(BaseModel):
    name: str
    pr_url: str
    message: str


class McpListResponse(BaseModel):
    mcps: list[McpStatusEntry]


# ---------------------------------------------------------------------------
# Registry — backed by a k8s ConfigMap in the praetor namespace
# ---------------------------------------------------------------------------

def _k8s_token() -> str | None:
    try:
        return open(_K8S_TOKEN_PATH).read().strip()
    except FileNotFoundError:
        return None


def _k8s_verify():
    ca = _K8S_CA_PATH
    if os.path.isfile(ca):
        return ca
    return True  # fallback to system CAs (e.g. localhost proxy)


def _migrate_entry(entry: dict) -> dict:
    """Promote a flat (pre-history) registry entry to versioned schema."""
    if "current" in entry:
        return entry
    return {"current": entry, "history": []}


async def _load_registry() -> dict[str, Any]:
    token = _k8s_token()
    if not token:
        logger.warning("no k8s service account token — registry in-memory only")
        return {}
    url = f"{K8S_API}/api/v1/namespaces/{REGISTRY_NS}/configmaps/{REGISTRY_CM}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(verify=_k8s_verify(), timeout=5) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            return {k: _migrate_entry(v) for k, v in json.loads(resp.json().get("data", {}).get("registry", "{}")).items()}
    except Exception as exc:
        logger.warning("failed to load mcp registry: %s", exc)
        return {}


async def _save_registry(registry: dict[str, Any]) -> None:
    token = _k8s_token()
    if not token:
        logger.warning("no k8s service account token — registry not persisted")
        return
    url = f"{K8S_API}/api/v1/namespaces/{REGISTRY_NS}/configmaps/{REGISTRY_CM}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": REGISTRY_CM, "namespace": REGISTRY_NS},
        "data": {"registry": json.dumps(registry)},
    }
    async with httpx.AsyncClient(verify=_k8s_verify(), timeout=5) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 404:
            create_url = f"{K8S_API}/api/v1/namespaces/{REGISTRY_NS}/configmaps"
            resp = await client.post(create_url, headers=headers, json=body)
        else:
            resp = await client.put(url, headers=headers, json=body)
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# GitHub Contents API helpers
# ---------------------------------------------------------------------------

async def _get_main_sha(gh: str, token: str) -> str:
    url = f"{GITHUB_API}/repos/{GITOPS_REPO}/git/ref/heads/main"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()["object"]["sha"]


async def _create_branch(gh: str, token: str, branch: str) -> None:
    main_sha = await _get_main_sha(gh, token)
    url = f"{GITHUB_API}/repos/{GITOPS_REPO}/git/refs"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url, json={"ref": f"refs/heads/{branch}", "sha": main_sha}, headers=headers,
        )
        if resp.status_code == 422:
            # branch already exists — ok
            return
        resp.raise_for_status()


async def _create_file(gh: str, token: str, path: str, content: str, message: str, branch: str) -> None:
    """Create or update a file in the gitops repo."""
    url = f"{GITHUB_API}/repos/{GITOPS_REPO}/contents/{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    # Check if file exists to get sha for updates
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params={"ref": branch}, headers=headers)
        if resp.status_code == 200:
            existing = resp.json()
            sha = existing["sha"]
            body = {
                "message": message,
                "content": base64.b64encode(content.encode()).decode(),
                "sha": sha,
                "branch": branch,
            }
        else:
            body = {
                "message": message,
                "content": base64.b64encode(content.encode()).decode(),
                "branch": branch,
            }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(url, headers=headers, json=body)
        if resp.status_code == 422 and "sha" in body:
            # File was modified concurrently — try again without sha (will fail properly)
            pass
        elif resp.status_code not in (200, 201):
            logger.error("mcp-factory: create_file failed (%s %s): %s", path, resp.status_code, resp.text[:200])


async def _update_file(gh: str, token: str, path: str, content: str, message: str, branch: str, sha: str) -> None:
    """Update a file in the gitops repo."""
    url = f"{GITHUB_API}/repos/{GITOPS_REPO}/contents/{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    body = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "sha": sha,
        "branch": branch,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(url, headers=headers, json=body)
        if resp.status_code not in (200, 201):
            logger.error("mcp-factory: update_file failed (%s %s): %s", path, resp.status_code, resp.text[:200])


async def _get_file(gh: str, token: str, path: str, branch: str) -> tuple[str, str]:
    """Get a file from the gitops repo. Returns (content, sha)."""
    url = f"{GITHUB_API}/repos/{GITOPS_REPO}/contents/{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params={"ref": branch}, headers=headers)
        if resp.status_code == 404:
            return "", ""
        data = resp.json()
        content = base64.b64decode(data["content"]).decode()
        sha = data["sha"]
    return content, sha


async def _get_or_create_pr(gh: str, token: str, title: str, body: str, head: str) -> str:
    """Open or return existing PR for the given branch."""
    url = f"{GITHUB_API}/repos/{GITOPS_REPO}/pulls"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    async with httpx.AsyncClient(timeout=15) as client:
        # Check existing PRs
        resp = await client.get(url, params={"head": f"{gh}:{head}", "state": "open"}, headers=headers)
        if resp.status_code == 200 and resp.json():
            return resp.json()[0]["html_url"]

        org = GITOPS_REPO.split("/")[0]
        body_params = {
            "title": title,
            "body": body,
            "head": f"{org}:{head}",
            "base": "main",
            "draft": True,
        }
        resp = await client.post(url, headers=headers, json=body_params)
        if resp.status_code == 422:
            # PR might exist under different state — try open anyway
            return f"https://github.com/{GITOPS_REPO}/pulls"
        resp.raise_for_status()
        return resp.json()["html_url"]


# ---------------------------------------------------------------------------
# LiteLLM config helpers
# ---------------------------------------------------------------------------

def _upsert_litellm_config(cm_content: str, reg: McpRegistration) -> tuple[str, bool]:
    """Insert or replace the MCP entry in the mcp_servers block. Returns (content, changed).

    - If the entry already exists: replace its url/transport lines in-place (idempotent).
    - If not: insert it immediately before the litellm_settings block.
    - Fallback (no litellm_settings marker): append to end.
    """
    new_entry = _litellm_mcp_entry(reg)
    entry_key = f"      {reg.name}:"  # 6-space indent matches mcp_servers children

    lines = cm_content.split("\n")
    for i, line in enumerate(lines):
        if line == entry_key:
            # Consume this line + all following 8-space-indented lines (url, transport, etc.)
            end = i + 1
            while end < len(lines) and lines[end].startswith("        "):
                end += 1
            new_lines = lines[:i] + new_entry.rstrip("\n").split("\n") + lines[end:]
            new_content = "\n".join(new_lines)
            return new_content, new_content != cm_content

    # Entry absent — insert before litellm_settings
    marker = "\n    litellm_settings:"
    if marker in cm_content:
        updated = cm_content.replace(marker, "\n" + new_entry + "    litellm_settings:", 1)
        return updated, True

    # Fallback: append (shouldn't happen with well-formed config)
    return cm_content.rstrip("\n") + "\n" + new_entry, True


# ---------------------------------------------------------------------------
# Manifest generators
# ---------------------------------------------------------------------------

def _deployment_yaml(reg: McpRegistration) -> str:
    env_block = ""
    if reg.env_vars or reg.env_secrets:
        lines = ["          env:"]
        for key, val in reg.env_vars.items():
            lines += [
                f"            - name: {key}",
                f"              value: {val!r}",
            ]
        for env_var in reg.env_secrets:
            lines += [
                f"            - name: {env_var}",
                f"              valueFrom:",
                f"                secretKeyRef:",
                f"                  name: {reg.name}-secrets",
                f"                  key: {env_var.lower()}",
            ]
        env_block = "\n".join(lines) + "\n"

    args_block = ""
    if reg.args:
        args_block = f"\n          args:\n{chr(10).join(f'            - {a}' for a in reg.args)}\n"

    probe_ready = ""
    if reg.health_path:
        probe_ready = (
            f"\n          readinessProbe:\n"
            f"            httpGet:\n"
            f"              path: {reg.health_path}\n"
            f"              port: {reg.port}\n"
            f"            initialDelaySeconds: 5\n"
            f"            periodSeconds: 10\n"
        )
    else:
        probe_ready = (
            f"\n          readinessProbe:\n"
            f"            tcpSocket:\n"
            f"              port: {reg.port}\n"
            f"            initialDelaySeconds: 5\n"
            f"            periodSeconds: 10\n"
        )

    probe_live = ""
    if reg.health_path:
        probe_live = (
            f"\n          livenessProbe:\n"
            f"            httpGet:\n"
            f"              path: {reg.health_path}\n"
            f"              port: {reg.port}\n"
            f"            initialDelaySeconds: 10\n"
            f"            periodSeconds: 30\n"
        )

    sa_block = ""
    if reg.service_account_name:
        sa_block = f"\n      serviceAccountName: {reg.service_account_name}"

    # Build nginx sidecar + volumes when host_rewrite is enabled
    sidecar_block = ""
    volumes_block = ""
    if reg.host_rewrite:
        sidecar_block = (
            f"        - name: host-proxy\n"
            f"          image: nginx:alpine\n"
            f"          ports:\n"
            f"            - containerPort: {_NGINX_PROXY_PORT}\n"
            f"          readinessProbe:\n"
            f"            tcpSocket:\n"
            f"              port: {_NGINX_PROXY_PORT}\n"
            f"            initialDelaySeconds: 2\n"
            f"            periodSeconds: 5\n"
            f"          volumeMounts:\n"
            f"            - name: nginx-conf\n"
            f"              mountPath: /etc/nginx/conf.d\n"
            f"              readOnly: true\n"
            f"          resources:\n"
            f"            requests:\n"
            f"              cpu: 5m\n"
            f"              memory: 16Mi\n"
            f"            limits:\n"
            f"              cpu: 50m\n"
            f"              memory: 32Mi\n"
        )
        volumes_block = (
            f"\n      volumes:\n"
            f"        - name: nginx-conf\n"
            f"          configMap:\n"
            f"            name: {reg.name}-nginx-proxy\n"
        )

    return (
        f"apiVersion: apps/v1\n"
        f"kind: Deployment\n"
        f"metadata:\n"
        f"  name: {reg.name}-server\n"
        f"  namespace: mcp-{reg.name}\n"
        f"spec:\n"
        f"  replicas: 1\n"
        f"  selector:\n"
        f"    matchLabels:\n"
        f"      app: {reg.name}-server\n"
        f"{sa_block}"
        f"  template:\n"
        f"    metadata:\n"
        f"      labels:\n"
        f"        app: {reg.name}-server\n"
        f"    spec:\n"
        f"      containers:\n"
        f"        - name: server\n"
        f"          image: {reg.image}\n"
        f"          imagePullPolicy: Always\n"
        f"          ports:\n"
        f"            - containerPort: {reg.port}\n"
        f"{env_block}"
        f"{args_block}"
        f"{probe_ready}"
        f"{probe_live}"
        f"          resources:\n"
        f"            requests:\n"
        f"              cpu: 10m\n"
        f"              memory: 64Mi\n"
        f"            limits:\n"
        f"              cpu: 100m\n"
        f"              memory: 128Mi\n"
        f"{sidecar_block}"
        f"{volumes_block}"
    )


def _service_account_yaml(reg: McpRegistration) -> str:
    return (
        f"apiVersion: v1\n"
        f"kind: ServiceAccount\n"
        f"metadata:\n"
        f"  name: {reg.service_account_name}\n"
        f"  namespace: mcp-{reg.name}\n"
    )


def _cluster_role_binding_yaml(reg: McpRegistration) -> str:
    binding_name = f"{reg.name}-{reg.cluster_role}-binding"
    return (
        f"apiVersion: rbac.authorization.io/v1\n"
        f"kind: ClusterRoleBinding\n"
        f"metadata:\n"
        f"  name: {binding_name}\n"
        f"roleRef:\n"
        f"  apiGroup: rbac.authorization.io\n"
        f"  kind: ClusterRole\n"
        f"  name: {reg.cluster_role}\n"
        f"subjects:\n"
        f"  - kind: ServiceAccount\n"
        f"    name: {reg.service_account_name}\n"
        f"    namespace: mcp-{reg.name}\n"
    )


def _service_yaml(reg: McpRegistration) -> str:
    return (
        f"apiVersion: v1\n"
        f"kind: Service\n"
        f"metadata:\n"
        f"  name: {reg.name}-server\n"
        f"  namespace: mcp-{reg.name}\n"
        f"spec:\n"
        f"  selector:\n"
        f"    app: {reg.name}-server\n"
        f"  ports:\n"
        f"    - port: {reg.port}\n"
        f"      targetPort: {_NGINX_PROXY_PORT if reg.host_rewrite else reg.port}\n"
        f"      protocol: TCP\n"
    )


def _externalsecret_yaml(reg: McpRegistration) -> str:
    data_entries = "\n".join(
        f"    - secretKey: {env_var.lower()}\n"
        f"      remoteRef:\n"
        f"        key: {bws_name}"
        for env_var, bws_name in reg.env_secrets.items()
    )
    return (
        f"apiVersion: external-secrets.io/v1\n"
        f"kind: ExternalSecret\n"
        f"metadata:\n"
        f"  name: {reg.name}-secrets\n"
        f"  namespace: mcp-{reg.name}\n"
        f"spec:\n"
        f"  refreshInterval: 1h\n"
        f"  secretStoreRef:\n"
        f"    name: bitwarden-secretstore\n"
        f"    kind: ClusterSecretStore\n"
        f"  target:\n"
        f"    name: {reg.name}-secrets\n"
        f"    creationPolicy: Owner\n"
        f"  data:\n"
        f"{data_entries}"
    )


def _argocd_application_yaml(reg: McpRegistration) -> str:
    return (
        f"\n---\n"
        f"# Application: {reg.name} MCP server (registered via MCP factory)\n"
        f"apiVersion: argoproj.io/v1alpha1\n"
        f"kind: Application\n"
        f"metadata:\n"
        f"  name: app-{reg.name}-server\n"
        f"  namespace: default\n"
        f"  annotations:\n"
        f"    argocd.argoproj.io/sync-wave: \"5\"\n"
        f"  finalizers:\n"
        f"    - resources-finalizer.argocd.argoproj.io/background\n"
        f"spec:\n"
        f"  project: application\n"
        f"  source:\n"
        f"    repoURL: https://github.com/amerenda/k3s-dean-gitops.git\n"
        f"    targetRevision: main\n"
        f"    path: apps/mcp/{reg.name}\n"
        f"  destination:\n"
        f"    server: https://kubernetes.default.svc\n"
        f"    namespace: mcp-{reg.name}\n"
        f"  syncPolicy:\n"
        f"    automated:\n"
        f"      prune: true\n"
        f"      selfHeal: true\n"
        f"    syncOptions:\n"
        f"      - CreateNamespace=true\n"
        f"      - PrunePropagationPolicy=foreground\n"
        f"    retry:\n"
        f"      limit: 5\n"
        f"      backoff:\n"
        f"        duration: 5s\n"
        f"        factor: 2\n"
        f"        maxDuration: 3m\n"
    )


def _litellm_mcp_entry(reg: McpRegistration) -> str:
    svc = f"{reg.name}-server.mcp-{reg.name}.svc.cluster.local"
    return (
        f"      {reg.name}:\n"
        f'        url: "http://{svc}:{reg.port}/mcp"\n'
        f'        transport: "{reg.transport}"\n'
    )


# ---------------------------------------------------------------------------
# nginx sidecar helpers (host_rewrite)
# ---------------------------------------------------------------------------

def _nginx_configmap_yaml(reg: McpRegistration) -> str:
    """Generate a k8s ConfigMap with an nginx proxy that rewrites Host header."""
    return (
        f"apiVersion: v1\n"
        f"kind: ConfigMap\n"
        f"metadata:\n"
        f"  name: {reg.name}-nginx-proxy\n"
        f"  namespace: mcp-{reg.name}\n"
        f"data:\n"
        f"  default.conf: |\n"
        f"    server {{\n"
        f"      listen {_NGINX_PROXY_PORT};\n"
        f"      location / {{\n"
        f'        proxy_pass http://127.0.0.1:{reg.port};\n'
        f"        proxy_set_header Host localhost;\n\n"
        f"        # HTTP/1.1 + empty Connection header = keep-alive to upstream\n"
        f"        proxy_http_version 1.1;\n"
        f'        proxy_set_header Connection "";\n'
        f"\n"
        f"        # Accommodate long-running tool calls (list/watch, logs)\n"
        f"        proxy_read_timeout 300s;\n"
        f"      }}\n"
        f"    }}\n"
    )


# ---------------------------------------------------------------------------
# Manifest builders
# ---------------------------------------------------------------------------

def _build_manifests(reg: McpRegistration) -> dict[str, str]:
    """Generate all k8s manifests for an MCP registration into an in-memory dict."""
    manifests: dict[str, str] = {
        "deployment.yaml": _deployment_yaml(reg),
        "service.yaml": _service_yaml(reg),
    }
    if reg.env_secrets:
        manifests["externalsecret.yaml"] = _externalsecret_yaml(reg)
    if reg.service_account_name and reg.cluster_role:
        manifests["serviceaccount.yaml"] = _service_account_yaml(reg)
        manifests["clusterrolebinding.yaml"] = _cluster_role_binding_yaml(reg)
    if reg.host_rewrite:
        manifests["nginx-proxy-configmap.yaml"] = _nginx_configmap_yaml(reg)
    return manifests


# ---------------------------------------------------------------------------
# Pre-PR inline review loop
# ---------------------------------------------------------------------------

async def _call_review_llm(manifests: dict[str, str], reg: McpRegistration) -> tuple[bool, list[str], dict[str, str]]:
    """Call the LiteLLM review endpoint to validate manifests."""
    system_prompt = get_system_prompt("pre-review-system", "production") or _PRE_REVIEW_SYSTEM_FALLBACK

    # Build manifest text for LLM
    manifest_text = ""
    for fname, content in sorted(manifests.items()):
        manifest_text += f"\n### {fname}\n```yaml\n{content.strip()}\n```\n"

    payload = {
        "model": "openai/gpt-4o",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Review these MCP manifests for `{reg.name}`:\n\n{manifest_text}\n"},
        ],
        "max_tokens": 2048,
    }

    headers = {
        "Authorization": f"Bearer {_LITELLM_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{_LITELLM_BASE}/chat/completions", headers=headers, json=payload)
        if resp.status_code != 200:
            logger.warning("mcp-factory: pre-review LLM call failed (%s) — skipping", resp.status_code)
            return True, [], {}
        content = resp.json()["choices"][0]["message"]["content"] or ""
        content = content.strip()
        if content.startswith("```"):
            parts = content.split("```", 2)
            content = parts[1].lstrip("json").strip() if len(parts) > 1 else content
        data = json.loads(content)
        return (
            bool(data.get("approved", True)),
            [str(i) for i in data.get("issues", [])],
            {k: str(v) for k, v in data.get("fixes", {}).items()},
        )
    except Exception as exc:
        logger.warning("mcp-factory: pre-review failed (%s) — proceeding without review", exc)
        return True, [], {}


async def _pre_review_loop(
    manifests: dict[str, str], reg: McpRegistration
) -> tuple[dict[str, str], str | None]:
    """Run up to _MAX_REVIEW_ITERATIONS review-and-fix cycles in memory.

    Returns (final_manifests, warning_or_None). Warning is set if manifests
    were never approved after all iterations — PR is still opened with a note.
    """
    last_issues: list[str] = []
    for i in range(_MAX_REVIEW_ITERATIONS):
        approved, issues, fixes = await _call_review_llm(manifests, reg)
        if approved:
            logger.info("mcp-factory: pre-review approved on iteration %d", i + 1)
            return manifests, None

        last_issues = issues
        # Apply fixes from LLM response
        for fname, fixed_content in fixes.items():
            if fname in manifests:
                manifests[fname] = fixed_content

    warning = (
        f"Pre-review did not approve after {_MAX_REVIEW_ITERATIONS} iterations. "
        f"Last issues: {'; '.join(last_issues[:3])}"
    )
    logger.warning("mcp-factory: %s", warning)
    return manifests, warning


async def _smoke_test_mcp_endpoint(url: str) -> None:
    """Validate that a service has a working /mcp endpoint. Raises HTTPException(422) on failure."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        if resp.status_code == 404:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"MCP server at {url} returned 404 — server has no /mcp endpoint. "
                    "Register via REST or add an MCP adapter first."
                ),
            )
    except httpx.ConnectError as exc:
        raise HTTPException(status_code=422, detail=f"MCP server at {url} not reachable: {exc}")
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=422, detail=f"MCP server at {url} timed out: {exc}")


# ---------------------------------------------------------------------------
# Register MCP endpoint
# ---------------------------------------------------------------------------

@router.post("/api/v1/mcp/register", response_model=McpRegisterResponse, dependencies=[Depends(_check_auth)])
async def register_mcp(reg: McpRegistration) -> McpRegisterResponse:
    """Register a new MCP server. Creates manifests + ArgoCD app in gitops repo."""

    registry = await _load_registry()
    if reg.name in registry and not reg.skip_manifests:
        raise HTTPException(
            status_code=409,
            detail=(
                f"MCP '{reg.name}' is already registered. "
                "Pass skip_manifests=true to update the LiteLLM entry only."
            ),
        )

    if reg.secret_tiers:
        merged: dict[str, str] = {}
        for tier_name in reg.secret_tiers:
            tier = _SECRET_TIERS.get(tier_name)
            if tier is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown secret tier: {tier_name!r}. Valid: {list(_SECRET_TIERS)}",
                )
            for bws_name, secret_key in tier.items():
                env_var = secret_key.upper().replace("-", "_")
                merged[env_var] = bws_name
        reg = reg.model_copy(update={"env_secrets": {**merged, **reg.env_secrets}})

    gh = os.environ.get("GITHUB_APP_LOGIN", "praetor-coder")
    token = get_installation_token()

    if reg.skip_manifests:
        mcp_url = f"http://{reg.name}-server.mcp-{reg.name}.svc.cluster.local:{reg.port}/mcp"
        await _smoke_test_mcp_endpoint(mcp_url)

    branch = f"feat/mcp-register-{reg.name}"
    pr_url = ""
    warning = None

    if not reg.skip_manifests:
        # Generate manifests (possibly with LLM review fixes)
        manifests = _build_manifests(reg)
        if not reg.skip_pre_review:
            manifests, warning = await _pre_review_loop(manifests, reg)
        else:
            warning = None

        # Create branch and push manifests
        await _create_branch(gh, token, branch)

        for fname, content in sorted(manifests.items()):
            path = f"apps/mcp/{reg.name}/{fname}"
            message = f"feat(mcp-factory): add {fname} for {reg.name}"
            if fname.endswith(".yaml") or fname.endswith(".yml"):
                await _create_file(gh, token, path, content, message, branch)

        # Create the ArgoCD application entry in root-app.yaml
        root_app, root_sha = await _get_file(gh, token, "root-app.yaml", branch)
        await _update_file(
            gh, token, "root-app.yaml",
            root_app + _argocd_application_yaml(reg),
            f"feat(mcp-factory): register {reg.name} in ArgoCD",
            branch, root_sha,
        )

    # Always upsert the LiteLLM configmap entry — idempotent, correct placement.
    cm_path = "apps/litellm/server/configmap.yaml"
    cm_content, cm_sha = await _get_file(gh, token, cm_path, branch)
    new_cm_content, cm_changed = _upsert_litellm_config(cm_content, reg)
    if cm_changed:
        await _update_file(
            gh, token, cm_path,
            new_cm_content,
            f"feat(mcp-factory): upsert {reg.name} in LiteLLM mcp_servers",
            branch, cm_sha,
        )

    mode = "LiteLLM registration only" if reg.skip_manifests else "full registration"
    pr_body = (
        f"Auto-generated by praetor MCP factory ({mode}).\n\n"
        f"Registers `{reg.name}` MCP:\n"
        f"- Image: `{reg.image}`\n"
        f"- Port: `{reg.port}`\n"
        f"- Transport: `{reg.transport}`\n"
        f"- Initial replicas: `1` (set to 0 to pause)\n"
    )
    if reg.image.endswith(":latest"):
        pr_body += "\n⚠️ **Image pinning:** `image` ends in `:latest` — consider pinning to a digest for reproducible rollbacks.\n"
    if warning:
        pr_body += f"\n---\n⚠️ **Pre-review warning:** {warning}\n"

    pr_url = await _get_or_create_pr(
        gh, token,
        f"feat(mcp-factory): register {reg.name} MCP",
        pr_body,
        branch,
    )

    existing_entry = registry.get(reg.name, {})
    old_current = existing_entry.get("current")
    history = existing_entry.get("history", [])
    if old_current and old_current.get("image") != reg.image:
        old_current["deregistered_at"] = datetime.now(timezone.utc).isoformat()
        history = [old_current] + history

    registry[reg.name] = {
        "current": {
            "name": reg.name,
            "image": reg.image,
            "port": reg.port,
            "transport": reg.transport,
            "pr_url": pr_url,
            "status": "pending",
            "service_account_name": reg.service_account_name,
            "cluster_role": reg.cluster_role,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        },
        "history": history,
    }
    try:
        await _save_registry(registry)
    except Exception as exc:
        logger.error("mcp-factory: registry save failed (PR %s already opened): %s", pr_url, exc)

    logger.info("mcp-factory: registered %s → %s", reg.name, pr_url)
    return McpRegisterResponse(
        name=reg.name,
        pr_url=pr_url,
        message=f"MCP '{reg.name}' registration PR created. Merge to deploy.",
    )


@router.get("/api/v1/mcp", response_model=McpListResponse, dependencies=[Depends(_check_auth)])
async def list_mcps() -> McpListResponse:
    registry = await _load_registry()
    return McpListResponse(mcps=[McpStatusEntry(**v["current"]) for v in registry.values()])


@router.get("/api/v1/mcp/{name}/history", dependencies=[Depends(_check_auth)])
async def get_mcp_history(name: str) -> McpHistoryResponse:
    registry = await _load_registry()
    if name not in registry:
        raise HTTPException(status_code=404, detail=f"MCP '{name}' not found in registry")
    entry = registry[name]
    return McpHistoryResponse(
        name=name,
        current=McpStatusEntry(**entry["current"]),
        history=[McpHistoryEntry(**h) for h in entry.get("history", [])],
    )


@router.post("/api/v1/mcp/{name}/rollback", dependencies=[Depends(_check_auth)])
async def rollback_mcp(name: str) -> McpRegisterResponse:
    """Re-register using the most recent history entry's image."""
    registry = await _load_registry()
    if name not in registry:
        raise HTTPException(status_code=404, detail=f"MCP '{name}' not found in registry")
    entry = registry[name]
    history = entry.get("history", [])
    if not history:
        raise HTTPException(status_code=409, detail=f"MCP '{name}' has no rollback history")

    prev = history[0]
    del registry[name]
    await _save_registry(registry)

    rollback_reg = McpRegistration(
        name=name,
        image=prev["image"],
        port=prev.get("port", 8000),
        transport=prev.get("transport", "http"),
        skip_pre_review=True,
    )
    return await register_mcp(rollback_reg)


# ---------------------------------------------------------------------------
# Delete MCP endpoint — full GitOps deregistration
# ---------------------------------------------------------------------------

async def _delete_file(token: str, path: str, sha: str, message: str, branch: str) -> None:
    """Delete a file from the gitops repo via GitHub Contents API."""
    url = f"{GITHUB_API}/repos/{GITOPS_REPO}/contents/{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    body = {"message": message, "sha": sha, "branch": branch}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(url, headers=headers, json=body)
    if resp.status_code not in (200, 201, 204, 404):
        logger.warning("mcp-factory: delete_file failed (%s %s): %s", path, resp.status_code, resp.text[:200])


async def _list_gitops_dir(token: str, path: str, branch: str) -> list[tuple[str, str]]:
    """List files in a gitops repo directory. Returns [(path, sha), ...] or [] if not found."""
    url = f"{GITHUB_API}/repos/{GITOPS_REPO}/contents/{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params={"ref": branch}, headers=headers)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return [(f["path"], f["sha"]) for f in resp.json() if f.get("type") == "file"]


def _remove_argocd_block(content: str, name: str) -> str:
    """Remove the ArgoCD Application block for {name} from root-app.yaml content.

    The block starts with "\\n---\\n# Application: {name} MCP server" and extends to
    the next "\\n---" or EOF.
    """
    pattern = rf"\n---\n# Application: {re.escape(name)} MCP server.*?(?=\n---|\Z)"
    cleaned = re.sub(pattern, "", content, flags=re.DOTALL)
    return cleaned


def _remove_litellm_mcp_entry(content: str, name: str) -> str:
    """Remove the {name} entry from the mcp_servers block in the LiteLLM configmap."""
    pattern = rf"\n      {re.escape(name)}:\n        url: \"[^\"]+\"\n        transport: \"[^\"]+\""
    cleaned = re.sub(pattern, "", content)
    return cleaned


async def delete_mcp(name: str) -> dict:
    """Fully deregister an MCP — remove manifests from gitops repo via PR."""

    registry = await _load_registry()
    if name not in registry:
        raise HTTPException(status_code=404, detail=f"MCP '{name}' not found in registry")

    gh = os.environ.get("GITHUB_APP_LOGIN", "praetor-coder")
    token = get_installation_token()
    branch = f"feat/mcp-deregister-{name}"

    await _create_branch(gh, token, branch)

    for fpath, fsha in await _list_gitops_dir(token, f"apps/mcp/{name}", branch):
        await _delete_file(
            token, fpath, fsha,
            f"chore(mcp-factory): remove {fpath} for deregistered MCP '{name}'",
            branch,
        )

    root_app, root_sha = await _get_file(gh, token, "root-app.yaml", branch)
    cleaned_root = _remove_argocd_block(root_app, name)
    if cleaned_root != root_app:
        await _update_file(gh, token, "root-app.yaml", cleaned_root,
                           f"chore(mcp-factory): remove ArgoCD block for '{name}'",
                           branch, root_sha)

    cm_path = "apps/litellm/server/configmap.yaml"
    cm_content, cm_sha = await _get_file(gh, token, cm_path, branch)
    cleaned_cm = _remove_litellm_mcp_entry(cm_content, name)
    if cleaned_cm != cm_content:
        await _update_file(gh, token, cm_path, cleaned_cm,
                           f"chore(mcp-factory): remove '{name}' from LiteLLM mcp_servers",
                           branch, cm_sha)

    pr_url = await _get_or_create_pr(
        gh, token,
        f"chore(mcp): deregister {name}",
        (
            f"Auto-generated by praetor MCP factory.\n\n"
            f"Deregisters `{name}` MCP:\n"
            f"- Removes all manifests under `apps/mcp/{name}/`\n"
            f"- Removes ArgoCD Application block from root-app.yaml\n"
            f"- Removes LiteLLM mcp_servers entry"
        ),
        branch,
    )

    del registry[name]
    await _save_registry(registry)

    logger.info("mcp-factory: deregistered %s → PR %s", name, pr_url)
    return {
        "deleted": name,
        "pr_url": pr_url,
        "note": "Merge PR to complete deregistration (ArgoCD will remove the namespace)",
    }


@router.delete("/api/v1/mcp/{name}", dependencies=[Depends(_check_auth)])
async def delete_mcp_route(name: str) -> dict:
    """Delete/deregister an MCP server."""
    return await delete_mcp(name)
