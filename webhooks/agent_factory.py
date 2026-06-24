"""FastAPI router: self-service agent factory — create and deploy Hatchet agents."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, field_validator

from common.github_app import get_installation_token
from common.langfuse_tools import create_prompt

logger = logging.getLogger(__name__)
router = APIRouter()
_bearer = HTTPBearer(auto_error=False)

K8S_API = "https://kubernetes.default.svc"
_K8S_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_K8S_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
AGENT_REGISTRY_CM = "agent-registry"
REGISTRY_NS = "praetor"
PRAETOR_REPO = "amerenda/praetor"
GITOPS_REPO = "amerenda/k3s-dean-gitops"
GITHUB_API = "https://api.github.com"
_KEBAB_RE = re.compile(r"^[a-z][a-z0-9-]{1,61}[a-z0-9]$")
_EVENT_RE = re.compile(r"^[a-z][a-z0-9:_-]{1,63}$")

_BASE_SECRET_KEYS = [
    ("hatchet-token", "hatchet-worker-api-token"),
    ("mem0-api-key", "mem0-admin-api-key"),
    ("litellm-api-key", "litellm-master-key"),
    ("vikunja-token", "vikjuna-api-key-full-access"),
    ("langfuse-public-key", "langfuse-public-key"),
    ("langfuse-secret-key", "langfuse-secret-key"),
]

_CODER_SECRET_KEYS = [
    ("coder-app-id", "github-amerenda-coder-app-id"),
    ("coder-private-key", "github-amerenda-coder-private-key"),
    ("coder-installation-id", "github-amerenda-coder-installation-id"),
]


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

class AgentCreateRequest(BaseModel):
    name: str
    description: str
    event: str
    tools: list[str] = []
    include_coder_creds: bool = False

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _KEBAB_RE.match(v):
            raise ValueError("name must be lowercase kebab-case, 3–63 chars")
        return v

    @field_validator("event")
    @classmethod
    def _validate_event(cls, v: str) -> str:
        if not _EVENT_RE.match(v):
            raise ValueError("event must be lowercase with optional colons/underscores/hyphens")
        return v


class AgentCreateResponse(BaseModel):
    agent: str
    event: str
    scaffold_pr_url: str | None = None
    manifest_pr_url: str | None = None
    langfuse_prompt: str | None = None
    pod_status: str = "unknown"
    smoke_test: str = "skipped"
    status: str
    message: str


# ---------------------------------------------------------------------------
# Skeleton generators
# ---------------------------------------------------------------------------

def _agent_py(name: str, description: str, tools: list[str]) -> str:
    name_py = name.replace("-", "_")
    mem_tools = [t for t in tools if t in ("search_memory", "add_memory")]
    mem_import = f"from common.memory_tools import {', '.join(mem_tools)}\n" if mem_tools else ""
    tool_list = ", ".join(tools)
    fallback = f"You are {name}, a Hatchet agent. {description}"
    return f'''"""PydanticAI agent: {description}"""
import os
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
{mem_import}from common.langfuse_tools import get_system_prompt

_SYSTEM_PROMPT_FALLBACK = """{fallback}"""


def build_agent() -> Agent:
    model = OpenAIChatModel(
        model_name=os.environ.get("LLM_MODEL", "{name}"),
        provider=OpenAIProvider(
            base_url=os.environ["LITELLM_BASE_URL"],
            api_key=os.environ["LITELLM_API_KEY"],
        ),
    )
    return Agent(
        model=model,
        system_prompt=get_system_prompt("{name}-system", fallback=_SYSTEM_PROMPT_FALLBACK),
        tools=[{tool_list}],
    )
'''


def _worker_py(name: str, event: str) -> str:
    name_py = name.replace("-", "_")
    class_name = "".join(w.capitalize() for w in name.split("-"))
    return f'''"""Hatchet worker: handles {event} events."""
import time
from datetime import timedelta

from hatchet_sdk import Context, Hatchet
from pydantic import BaseModel
from pydantic_ai.usage import UsageLimits

from .agent import build_agent
from common.langfuse_tools import langfuse_context, observe
from common.memory_tools import add_memory, search_memory
from common.metrics import start_metrics_server, task_invocations, task_active, task_duration

_agent = None
_AGENT_NAME = "{name}"


def _get_agent():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


class {class_name}Input(BaseModel):
    task_title: str
    canary: bool = False


@observe(capture_input=False, capture_output=False)
async def _run_{name_py}(input: {class_name}Input, context: Context) -> dict:
    langfuse_context.update_current_trace(
        name=f"{name}-{{input.task_title}}",
        input=input.model_dump(),
        tags=["{name}"],
    )
    prior = await search_memory(input.task_title, "{name}")
    prior_context = "\\n".join(prior) if prior else "No prior memory found."
    prompt = f"Task: {{input.task_title}}\\n\\nPrior context:\\n{{prior_context}}"
    agent = _get_agent()
    task_active.labels(agent=_AGENT_NAME).inc()
    t0 = time.monotonic()
    try:
        result = await agent.run(prompt, usage_limits=UsageLimits(request_limit=50))
        task_invocations.labels(agent=_AGENT_NAME, status="success").inc()
        langfuse_context.update_current_trace(output=result.output)
        await add_memory(f"{{input.task_title}}: {{result.output}}", "{name}")
        return {{"result": result.output}}
    except Exception:
        task_invocations.labels(agent=_AGENT_NAME, status="error").inc()
        raise
    finally:
        task_active.labels(agent=_AGENT_NAME).dec()
        task_duration.labels(agent=_AGENT_NAME).observe(time.monotonic() - t0)


def main() -> None:
    start_metrics_server(_AGENT_NAME)
    hatchet = Hatchet()
    run_{name_py} = hatchet.task(
        name="{name}",
        on_events=["{event}"],
        execution_timeout=timedelta(minutes=20),
        retries=1,
    )(_run_{name_py})
    worker = hatchet.worker("{name}-worker", workflows=[run_{name_py}], slots=1)
    worker.start()


if __name__ == "__main__":
    main()
'''


def _dockerfile(name: str) -> str:
    name_py = name.replace("-", "_")
    return f"""FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 appuser
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/ common/
COPY agents/ agents/
RUN mkdir -p /scratch && chown appuser:appuser /scratch
ENV PYTHONUNBUFFERED=1
ENV SCRATCH_DIR=/scratch
USER appuser
CMD ["python", "-m", "agents.{name_py}.worker"]
"""


def _deployment_yaml(name: str, image_tag: str) -> str:
    secret_name = f"praetor-{name}-secrets"

    def _secret_env(env_name: str, key: str) -> str:
        return (
            f"            - name: {env_name}\n"
            f"              valueFrom:\n"
            f"                secretKeyRef:\n"
            f"                  name: {secret_name}\n"
            f"                  key: {key}\n"
        )

    def _plain_env(env_name: str, value: str) -> str:
        return (
            f"            - name: {env_name}\n"
            f'              value: "{value}"\n'
        )

    return (
        f"apiVersion: apps/v1\n"
        f"kind: Deployment\n"
        f"metadata:\n"
        f"  name: praetor-{name}-worker\n"
        f"  namespace: praetor\n"
        f"spec:\n"
        f"  replicas: 1\n"
        f"  selector:\n"
        f"    matchLabels:\n"
        f"      app: praetor-{name}-worker\n"
        f"  strategy:\n"
        f"    type: RollingUpdate\n"
        f"    rollingUpdate:\n"
        f"      maxUnavailable: 0\n"
        f"      maxSurge: 1\n"
        f"  template:\n"
        f"    metadata:\n"
        f"      labels:\n"
        f"        app: praetor-{name}-worker\n"
        f"    spec:\n"
        f"      nodeSelector:\n"
        f"        kubernetes.io/arch: amd64\n"
        f"      securityContext:\n"
        f"        runAsUser: 1000\n"
        f"        runAsGroup: 1000\n"
        f"        fsGroup: 1000\n"
        f"      containers:\n"
        f"        - name: {name}-worker\n"
        f"          image: amerenda/praetor-{name}:{image_tag}\n"
        f"          imagePullPolicy: Always\n"
        f"          securityContext:\n"
        f"            readOnlyRootFilesystem: true\n"
        f"            allowPrivilegeEscalation: false\n"
        f"            runAsNonRoot: true\n"
        f"          env:\n"
        + _secret_env("HATCHET_CLIENT_TOKEN", "hatchet-token")
        + _plain_env("HATCHET_CLIENT_TLS_STRATEGY", "none")
        + _plain_env("MEM0_BASE_URL", "https://mem0.amer.dev")
        + _secret_env("MEM0_API_KEY", "mem0-api-key")
        + _secret_env("VIKUNJA_TOKEN", "vikunja-token")
        + _plain_env("VIKUNJA_BASE_URL", "https://todo.amer.dev")
        + _plain_env("LITELLM_BASE_URL", "https://litellm.amer.dev/v1")
        + _secret_env("LITELLM_API_KEY", "litellm-api-key")
        + _plain_env("LLM_MODEL", name)
        + _plain_env("SCRATCH_DIR", "/scratch")
        + _plain_env("LITELLM_MCP_URL", "https://litellm.amer.dev/mcp/")
        + _plain_env("LANGFUSE_HOST", "https://langfuse.amer.dev")
        + _secret_env("LANGFUSE_PUBLIC_KEY", "langfuse-public-key")
        + _secret_env("LANGFUSE_SECRET_KEY", "langfuse-secret-key")
        + "          volumeMounts:\n"
        "            - name: scratch\n"
        "              mountPath: /scratch\n"
        "            - name: home\n"
        "              mountPath: /home/appuser\n"
        "          resources:\n"
        "            requests:\n"
        "              cpu: 100m\n"
        "              memory: 512Mi\n"
        "            limits:\n"
        "              cpu: 2000m\n"
        "              memory: 4Gi\n"
        "      volumes:\n"
        "        - name: scratch\n"
        "          emptyDir:\n"
        "            sizeLimit: 4Gi\n"
        "        - name: home\n"
        "          emptyDir: {}\n"
    )


def _externalsecret_yaml(name: str, include_coder_creds: bool = False) -> str:
    secret_name = f"praetor-{name}-secrets"
    keys = list(_BASE_SECRET_KEYS)
    if include_coder_creds:
        keys.extend(_CODER_SECRET_KEYS)
    data_entries = "".join(
        f"    - secretKey: {k8s_key}\n"
        f"      remoteRef:\n"
        f"        key: {bws_key}\n"
        f"        property: password\n"
        for k8s_key, bws_key in keys
    )
    return (
        f"apiVersion: external-secrets.io/v1\n"
        f"kind: ExternalSecret\n"
        f"metadata:\n"
        f"  name: {secret_name}\n"
        f"  namespace: praetor\n"
        f"spec:\n"
        f"  refreshInterval: 1h\n"
        f"  secretStoreRef:\n"
        f"    name: bitwarden-secretstore\n"
        f"    kind: ClusterSecretStore\n"
        f"  target:\n"
        f"    name: {secret_name}\n"
        f"    creationPolicy: Owner\n"
        f"  data:\n"
        f"{data_entries}"
    )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _extract_pr_number(pr_url: str) -> int:
    return int(pr_url.rstrip("/").rsplit("/", 1)[-1])


def _render_system_prompt(name: str, description: str, tools: list[str]) -> str:
    return (
        f"You are {name}, a Hatchet agent. {description}\n\n"
        f"Available tools: {', '.join(tools) if tools else 'none'}."
    )


# ---------------------------------------------------------------------------
# K8s helpers
# ---------------------------------------------------------------------------

def _k8s_token() -> str | None:
    try:
        return open(_K8S_TOKEN_PATH).read().strip()
    except FileNotFoundError:
        return None


def _k8s_verify() -> str | bool:
    return _K8S_CA_PATH if os.path.exists(_K8S_CA_PATH) else False


# ---------------------------------------------------------------------------
# GitHub helpers (repo-parameterised — parallel to mcp_factory helpers)
# ---------------------------------------------------------------------------

def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _get_main_sha(client: httpx.AsyncClient, token: str, repo: str) -> str:
    resp = await client.get(
        f"{GITHUB_API}/repos/{repo}/git/ref/heads/main",
        headers=_gh_headers(token),
    )
    resp.raise_for_status()
    return resp.json()["object"]["sha"]


async def _create_branch(
    client: httpx.AsyncClient, token: str, repo: str, branch: str, sha: str
) -> None:
    resp = await client.post(
        f"{GITHUB_API}/repos/{repo}/git/refs",
        headers=_gh_headers(token),
        json={"ref": f"refs/heads/{branch}", "sha": sha},
    )
    resp.raise_for_status()


async def _create_file(
    client: httpx.AsyncClient,
    token: str,
    repo: str,
    path: str,
    content: str,
    message: str,
    branch: str,
) -> None:
    resp = await client.put(
        f"{GITHUB_API}/repos/{repo}/contents/{path}",
        headers=_gh_headers(token),
        json={
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        },
    )
    resp.raise_for_status()


async def _create_pr(
    client: httpx.AsyncClient, token: str, repo: str, title: str, body: str, head: str
) -> str:
    resp = await client.post(
        f"{GITHUB_API}/repos/{repo}/pulls",
        headers=_gh_headers(token),
        json={"title": title, "body": body, "head": head, "base": "main"},
    )
    resp.raise_for_status()
    return resp.json()["html_url"]


async def _merge_pr(
    client: httpx.AsyncClient, token: str, repo: str, pr_number: int, merge_method: str = "squash"
) -> str:
    resp = await client.post(
        f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/merge",
        headers=_gh_headers(token),
        json={"merge_method": merge_method},
    )
    resp.raise_for_status()
    return resp.json()["sha"]


# ---------------------------------------------------------------------------
# Registry helpers (agent-registry ConfigMap)
# ---------------------------------------------------------------------------

async def _load_agent_registry() -> dict:
    token = _k8s_token()
    if not token:
        logger.warning("no k8s service account token — agent registry unavailable")
        return {}
    url = f"{K8S_API}/api/v1/namespaces/{REGISTRY_NS}/configmaps/{AGENT_REGISTRY_CM}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(verify=_k8s_verify(), timeout=5) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            return json.loads(resp.json().get("data", {}).get("registry", "{}"))
    except Exception as exc:
        logger.warning("failed to load agent registry: %s", exc)
        return {}


async def _save_agent_registry(registry: dict) -> None:
    token = _k8s_token()
    if not token:
        logger.warning("no k8s service account token — agent registry not persisted")
        return
    url = f"{K8S_API}/api/v1/namespaces/{REGISTRY_NS}/configmaps/{AGENT_REGISTRY_CM}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": AGENT_REGISTRY_CM, "namespace": REGISTRY_NS},
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
# Polling helpers
# ---------------------------------------------------------------------------

async def _wait_for_pr_ci(
    client: httpx.AsyncClient, token: str, repo: str, pr_number: int, timeout: int = 300
) -> bool:
    resp = await client.get(
        f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
        headers=_gh_headers(token),
    )
    resp.raise_for_status()
    head_sha = resp.json()["head"]["sha"]

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = await client.get(
                f"{GITHUB_API}/repos/{repo}/commits/{head_sha}/check-runs",
                headers=_gh_headers(token),
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                # GitHub App lacks checks:read — wait 90s for CI to likely complete, then proceed
                logger.warning("checks:read permission missing on GitHub App — waiting 90s for CI then proceeding")
                await asyncio.sleep(90)
                return True
            raise
        runs = resp.json().get("check_runs", [])
        if runs and all(r["status"] == "completed" for r in runs):
            return all(r["conclusion"] == "success" for r in runs)
        await asyncio.sleep(15)
    return False


async def _wait_for_ci_run_complete(
    client: httpx.AsyncClient, token: str, merge_sha: str, timeout: int = 300
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = await client.get(
                f"{GITHUB_API}/repos/{PRAETOR_REPO}/actions/runs",
                params={"head_sha": merge_sha, "per_page": 5},
                headers=_gh_headers(token),
            )
            if resp.status_code == 403:
                # GitHub App lacks actions:read — wait remaining timeout as fixed delay
                logger.warning("actions:read permission missing on GitHub App — waiting for image build timeout")
                await asyncio.sleep(min(timeout, 240))
                return True
            for run in resp.json().get("workflow_runs", []):
                if run["name"] == "Build and Deploy" and run["status"] == "completed":
                    return run["conclusion"] == "success"
        except Exception as exc:
            logger.warning("_wait_for_ci_run_complete error: %s", exc)
        await asyncio.sleep(15)
    return False


async def _wait_for_pod(name: str, timeout: int = 300) -> str:
    token = _k8s_token()
    if not token:
        return "unknown"
    url = (
        f"{K8S_API}/api/v1/namespaces/praetor/pods"
        f"?labelSelector=app%3Dpraetor-{name}-worker"
    )
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(verify=_k8s_verify(), timeout=5) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                items = resp.json().get("items", [])
                for pod in items:
                    conditions = pod.get("status", {}).get("conditions", [])
                    for cond in conditions:
                        if cond.get("type") == "Ready" and cond.get("status") == "True":
                            return "ready"
        except Exception as exc:
            logger.debug("pod poll error: %s", exc)
        await asyncio.sleep(10)
    return "not_ready"


async def _smoke_test(event: str, timeout: int = 60) -> str:
    from hatchet_sdk import Hatchet, V1TaskStatus

    h = Hatchet()
    try:
        pushed = await h.event.aio_push(event, {"task_title": "smoke-test", "canary": True})
    except Exception as exc:
        logger.warning("smoke_test push failed: %s", exc)
        return "failed"

    deadline = time.monotonic() + timeout
    run_id: str | None = None
    while time.monotonic() < deadline:
        try:
            runs = await h.runs.aio_list(
                triggering_event_external_id=pushed.event_id, limit=1
            )
            if runs.rows:
                run_id = runs.rows[0].metadata.id
                break
        except Exception:
            pass
        await asyncio.sleep(5)

    if not run_id:
        return "dispatched"

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = await h.runs.aio_get_status(run_id)
            if status == V1TaskStatus.COMPLETED:
                return "passed"
            if status in (V1TaskStatus.FAILED, V1TaskStatus.CANCELLED):
                return "failed"
        except Exception:
            pass
        await asyncio.sleep(5)
    return "dispatched"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post(
    "/api/v1/agent/create",
    response_model=AgentCreateResponse,
    dependencies=[Depends(_check_auth)],
)
async def create_agent(req: AgentCreateRequest) -> AgentCreateResponse:
    registry = await _load_agent_registry()
    if req.name in registry:
        entry = registry[req.name]
        return AgentCreateResponse(
            agent=req.name,
            event=req.event,
            scaffold_pr_url=entry.get("scaffold_pr_url"),
            manifest_pr_url=entry.get("manifest_pr_url"),
            status="running",
            message=f"Agent '{req.name}' already exists.",
        )

    token = get_installation_token()

    # Step 1: generate skeleton in memory
    name_py = req.name.replace("-", "_")
    skeleton = {
        f"agents/{req.name}/__init__.py": "",
        f"agents/{req.name}/agent.py": _agent_py(req.name, req.description, req.tools),
        f"agents/{req.name}/worker.py": _worker_py(req.name, req.event),
        f"agents/{req.name}/Dockerfile": _dockerfile(req.name),
    }

    # Step 2: open scaffold PR in praetor repo
    scaffold_branch = f"feat/agent-{req.name}"
    scaffold_pr_url: str
    scaffold_pr_number: int
    try:
        async with httpx.AsyncClient(timeout=30) as gh:
            praetor_sha = await _get_main_sha(gh, token, PRAETOR_REPO)
            await _create_branch(gh, token, PRAETOR_REPO, scaffold_branch, praetor_sha)
            for path, content in skeleton.items():
                await _create_file(
                    gh, token, PRAETOR_REPO, path, content,
                    f"feat(agent-factory): add {req.name} agent", scaffold_branch,
                )
            scaffold_pr_url = await _create_pr(
                gh, token, PRAETOR_REPO,
                f"feat(agent-factory): add {req.name} agent",
                (
                    f"Agent factory scaffold for `{req.name}`.\n\n"
                    f"CI will build and push `amerenda/praetor-{req.name}` on merge."
                ),
                scaffold_branch,
            )
            scaffold_pr_number = _extract_pr_number(scaffold_pr_url)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"GitHub API error (scaffold): {exc.response.text[:200]}",
        )

    # Step 3: poll scaffold PR CI, merge, capture SHA
    merge_sha: str
    async with httpx.AsyncClient(timeout=30) as gh:
        ci_passed = await _wait_for_pr_ci(gh, token, PRAETOR_REPO, scaffold_pr_number)
        if not ci_passed:
            return AgentCreateResponse(
                agent=req.name, event=req.event,
                scaffold_pr_url=scaffold_pr_url,
                status="partial",
                message=f"Scaffold PR CI timed out. Merge manually: {scaffold_pr_url}",
            )
        try:
            merge_sha = await _merge_pr(gh, token, PRAETOR_REPO, scaffold_pr_number)
        except Exception as exc:
            return AgentCreateResponse(
                agent=req.name, event=req.event,
                scaffold_pr_url=scaffold_pr_url,
                status="partial",
                message=f"Scaffold PR merge failed: {exc}. Merge manually: {scaffold_pr_url}",
            )
    image_tag = f"sha-{merge_sha[:7]}"

    # Step 4: wait for CI image build (non-fatal on timeout)
    async with httpx.AsyncClient(timeout=30) as gh:
        await _wait_for_ci_run_complete(gh, token, merge_sha)

    # Step 5: open manifest PR in gitops repo and auto-merge
    manifest_pr_url: str
    async with httpx.AsyncClient(timeout=30) as gh:
        gitops_sha = await _get_main_sha(gh, token, GITOPS_REPO)
        manifest_branch = f"feat/agent-{req.name}-manifests"
        await _create_branch(gh, token, GITOPS_REPO, manifest_branch, gitops_sha)
        await _create_file(
            gh, token, GITOPS_REPO,
            f"apps/praetor/{req.name}-worker/deployment.yaml",
            _deployment_yaml(req.name, image_tag),
            f"feat(agent-factory): add {req.name} deployment",
            manifest_branch,
        )
        await _create_file(
            gh, token, GITOPS_REPO,
            f"apps/praetor/{req.name}-worker/externalsecret.yaml",
            _externalsecret_yaml(req.name, req.include_coder_creds),
            f"feat(agent-factory): add {req.name} externalsecret",
            manifest_branch,
        )
        manifest_pr_url = await _create_pr(
            gh, token, GITOPS_REPO,
            f"feat(agent-factory): deploy {req.name} agent",
            (
                f"Gitops manifest for `praetor-{req.name}-worker`.\n\n"
                f"Image: `amerenda/praetor-{req.name}:{image_tag}`"
            ),
            manifest_branch,
        )
        manifest_pr_number = _extract_pr_number(manifest_pr_url)
        await _merge_pr(gh, token, GITOPS_REPO, manifest_pr_number)

    # Steps 6-8: pod readiness, Langfuse prompt, smoke test
    pod_status = await _wait_for_pod(req.name)
    prompt_name = f"{req.name}-system"
    prompt_created = create_prompt(
        prompt_name, _render_system_prompt(req.name, req.description, req.tools)
    )
    smoke = await _smoke_test(req.event)

    # Step 9: persist in registry
    registry[req.name] = {
        "name": req.name,
        "event": req.event,
        "scaffold_pr_url": scaffold_pr_url,
        "manifest_pr_url": manifest_pr_url,
        "langfuse_prompt": prompt_name if prompt_created else None,
        "status": "running",
    }
    try:
        await _save_agent_registry(registry)
    except Exception as exc:
        logger.error("agent-factory: registry save failed: %s", exc)

    return AgentCreateResponse(
        agent=req.name,
        event=req.event,
        scaffold_pr_url=scaffold_pr_url,
        manifest_pr_url=manifest_pr_url,
        langfuse_prompt=prompt_name if prompt_created else None,
        pod_status=pod_status,
        smoke_test=smoke,
        status="running",
        message=(
            f"Agent '{req.name}' deployed. Event: {req.event}. "
            f"Prompt: {prompt_name}."
        ),
    )


@router.get("/api/v1/agent/{name}", dependencies=[Depends(_check_auth)])
async def get_agent(name: str) -> dict:
    registry = await _load_agent_registry()
    if name not in registry:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return registry[name]
