"""FastAPI router: self-service MCP factory — register, list, and delete MCPs."""
from __future__ import annotations

import base64
import json
import logging
import os
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
    skip_pre_review: bool = False            # bypass inline LLM review loop (e.g. for idempotent re-runs)


class McpStatusEntry(BaseModel):
    name: str
    image: str
    port: int
    transport: str
    pr_url: str | None = None
    status: str = "pending"
    service_account_name: str | None = None
    cluster_role: str | None = None


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


def _k8s_verify() -> str | bool:
    return _K8S_CA_PATH if os.path.exists(_K8S_CA_PATH) else False


async def _load_registry() -> dict[str, Any]:
    token = _k8s_token()
    if not token:
        logger.warning("no k8s service account token — registry unavailable")
        return {}
    url = f"{K8S_API}/api/v1/namespaces/{REGISTRY_NS}/configmaps/{REGISTRY_CM}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(verify=_k8s_verify(), timeout=5) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            return json.loads(resp.json().get("data", {}).get("registry", "{}"))
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

def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _get_main_sha(client: httpx.AsyncClient, token: str) -> str:
    resp = await client.get(
        f"{GITHUB_API}/repos/{GITOPS_REPO}/git/ref/heads/main",
        headers=_gh_headers(token),
    )
    resp.raise_for_status()
    return resp.json()["object"]["sha"]


async def _create_branch(client: httpx.AsyncClient, token: str, branch: str, sha: str) -> None:
    resp = await client.post(
        f"{GITHUB_API}/repos/{GITOPS_REPO}/git/refs",
        headers=_gh_headers(token),
        json={"ref": f"refs/heads/{branch}", "sha": sha},
    )
    resp.raise_for_status()


async def _get_file(client: httpx.AsyncClient, token: str, path: str, ref: str) -> tuple[str, str]:
    """Returns (decoded_content, blob_sha)."""
    resp = await client.get(
        f"{GITHUB_API}/repos/{GITOPS_REPO}/contents/{path}",
        headers=_gh_headers(token),
        params={"ref": ref},
    )
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"].replace("\n", "")).decode()
    return content, data["sha"]


async def _create_file(
    client: httpx.AsyncClient, token: str, path: str, content: str, message: str, branch: str
) -> None:
    resp = await client.put(
        f"{GITHUB_API}/repos/{GITOPS_REPO}/contents/{path}",
        headers=_gh_headers(token),
        json={
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        },
    )
    resp.raise_for_status()


async def _update_file(
    client: httpx.AsyncClient, token: str, path: str, content: str, message: str, branch: str, sha: str
) -> None:
    resp = await client.put(
        f"{GITHUB_API}/repos/{GITOPS_REPO}/contents/{path}",
        headers=_gh_headers(token),
        json={
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
            "sha": sha,
        },
    )
    resp.raise_for_status()


async def _create_pr(
    client: httpx.AsyncClient, token: str, title: str, body: str, head: str
) -> str:
    """Returns PR HTML URL."""
    resp = await client.post(
        f"{GITHUB_API}/repos/{GITOPS_REPO}/pulls",
        headers=_gh_headers(token),
        json={"title": title, "body": body, "head": head, "base": "main"},
    )
    resp.raise_for_status()
    return resp.json()["html_url"]


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
        args_block = "          args:\n" + "".join(f"            - {a!r}\n" for a in reg.args)

    sa_block = f"      serviceAccountName: {reg.service_account_name}\n" if reg.service_account_name else ""

    if reg.health_path:
        probe_ready = (
            f"          readinessProbe:\n"
            f"            httpGet:\n"
            f"              path: {reg.health_path}\n"
            f"              port: {reg.port}\n"
            f"            initialDelaySeconds: 5\n"
            f"            periodSeconds: 10\n"
        )
        probe_live = (
            f"          livenessProbe:\n"
            f"            httpGet:\n"
            f"              path: {reg.health_path}\n"
            f"              port: {reg.port}\n"
            f"            initialDelaySeconds: 10\n"
            f"            periodSeconds: 30\n"
        )
    else:
        probe_ready = (
            f"          readinessProbe:\n"
            f"            tcpSocket:\n"
            f"              port: {reg.port}\n"
            f"            initialDelaySeconds: 5\n"
            f"            periodSeconds: 10\n"
        )
        probe_live = (
            f"          livenessProbe:\n"
            f"            tcpSocket:\n"
            f"              port: {reg.port}\n"
            f"            initialDelaySeconds: 10\n"
            f"            periodSeconds: 30\n"
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
        f"  strategy:\n"
        f"    type: RollingUpdate\n"
        f"    rollingUpdate:\n"
        f"      maxUnavailable: 0\n"
        f"      maxSurge: 1\n"
        f"  template:\n"
        f"    metadata:\n"
        f"      labels:\n"
        f"        app: {reg.name}-server\n"
        f"    spec:\n"
        f"{sa_block}"
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
        f"apiVersion: rbac.authorization.k8s.io/v1\n"
        f"kind: ClusterRoleBinding\n"
        f"metadata:\n"
        f"  name: {binding_name}\n"
        f"roleRef:\n"
        f"  apiGroup: rbac.authorization.k8s.io\n"
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
        f"    - name: http\n"
        f"      port: {reg.port}\n"
        f"      targetPort: {reg.port}\n"
    )


def _externalsecret_yaml(reg: McpRegistration) -> str:
    data_entries = "".join(
        f"    - secretKey: {env_var.lower()}\n"
        f"      remoteRef:\n"
        f"        key: {bws_name}\n"
        f"        property: password\n"
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
# Pre-PR inline review loop
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
    return manifests


async def _call_review_llm(
    manifests: dict[str, str], reg: McpRegistration
) -> tuple[bool, list[str], dict[str, str]]:
    """Ask the LLM to review manifests. Returns (approved, issues, fixes).
    On any failure, returns (True, [], {}) so the pipeline continues unblocked.
    """
    manifest_text = "\n\n".join(f"# {name}\n{content}" for name, content in manifests.items())
    reg_ctx = f"name={reg.name} image={reg.image} port={reg.port} health_path={reg.health_path}"
    payload = {
        "model": os.environ.get("LLM_MODEL", "qwen3-35b"),
        "messages": [
            {"role": "system", "content": get_system_prompt("mcp-pre-review-system", fallback=_PRE_REVIEW_SYSTEM_FALLBACK)},
            {"role": "user", "content": f"Registration: {reg_ctx}\n\nManifests:\n{manifest_text}"},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 2048,
    }
    headers = {"Authorization": f"Bearer {_LITELLM_KEY}", "Content-Type": "application/json"}
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
        logger.info("mcp-factory: pre-review iteration %d issues: %s", i + 1, issues)
        if fixes:
            manifests = {**manifests, **fixes}
    warning = (
        f"Pre-review did not approve after {_MAX_REVIEW_ITERATIONS} iterations. "
        f"Last issues: {'; '.join(last_issues[:3])}"
    )
    logger.warning("mcp-factory: %s", warning)
    return manifests, warning


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

@router.post("/api/v1/mcp/register", response_model=McpRegisterResponse, dependencies=[Depends(_check_auth)])
async def register_mcp(reg: McpRegistration) -> McpRegisterResponse:
    registry = await _load_registry()
    if reg.name in registry:
        raise HTTPException(status_code=409, detail=f"MCP '{reg.name}' is already registered")

    # Generate manifests in memory, optionally run review loop before any GitHub writes
    manifests = _build_manifests(reg)
    review_warning: str | None = None
    if not reg.skip_pre_review:
        manifests, review_warning = await _pre_review_loop(manifests, reg)

    token = get_installation_token()
    branch = f"feat/mcp-register-{reg.name}"

    async with httpx.AsyncClient(timeout=30) as gh:
        main_sha = await _get_main_sha(gh, token)
        await _create_branch(gh, token, branch, main_sha)

        for filename, content in manifests.items():
            await _create_file(
                gh, token,
                f"apps/mcp/{reg.name}/{filename}",
                content,
                f"feat(mcp-factory): add {reg.name} MCP {filename}",
                branch,
            )

        root_app, root_sha = await _get_file(gh, token, "root-app.yaml", branch)
        await _update_file(
            gh, token, "root-app.yaml",
            root_app + _argocd_application_yaml(reg),
            f"feat(mcp-factory): register {reg.name} in ArgoCD",
            branch, root_sha,
        )

        cm_path = "apps/litellm/server/configmap.yaml"
        cm_content, cm_sha = await _get_file(gh, token, cm_path, branch)
        await _update_file(
            gh, token, cm_path,
            cm_content.rstrip("\n") + "\n" + _litellm_mcp_entry(reg),
            f"feat(mcp-factory): add {reg.name} to LiteLLM mcp_servers",
            branch, cm_sha,
        )

        pr_body = (
            f"Auto-generated by praetor MCP factory.\n\n"
            f"Registers `{reg.name}` MCP:\n"
            f"- Image: `{reg.image}`\n"
            f"- Port: `{reg.port}`\n"
            f"- Transport: `{reg.transport}`\n"
        )
        if review_warning:
            pr_body += f"\n---\n⚠️ **Pre-review warning:** {review_warning}\n"

        pr_url = await _create_pr(
            gh, token,
            f"feat(mcp-factory): register {reg.name} MCP",
            pr_body,
            branch,
        )

    registry[reg.name] = {
        "name": reg.name,
        "image": reg.image,
        "port": reg.port,
        "transport": reg.transport,
        "pr_url": pr_url,
        "status": "pending",
        "service_account_name": reg.service_account_name,
        "cluster_role": reg.cluster_role,
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
    return McpListResponse(mcps=[McpStatusEntry(**v) for v in registry.values()])


@router.delete("/api/v1/mcp/{name}", dependencies=[Depends(_check_auth)])
async def delete_mcp(name: str) -> dict:
    registry = await _load_registry()
    if name not in registry:
        raise HTTPException(status_code=404, detail=f"MCP '{name}' not found in registry")
    del registry[name]
    await _save_registry(registry)
    logger.info("mcp-factory: deregistered %s", name)
    return {"deleted": name, "note": "Registry entry removed. Remove manifests from k3s-dean-gitops manually."}
