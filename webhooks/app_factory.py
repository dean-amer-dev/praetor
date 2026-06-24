"""FastAPI router: full app pipeline — create repo, provision k3s manifests, dispatch coder."""
from __future__ import annotations

import logging
import os
import re
import time

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, field_validator

from common.dispatch import dispatch_agent
from common.github_app import get_installation_token

logger = logging.getLogger(__name__)
router = APIRouter()
_bearer = HTTPBearer(auto_error=False)

_KEBAB_RE = re.compile(r"^[a-z][a-z0-9-]{1,61}[a-z0-9]$")
_INFRA_MCP_BASE = os.environ.get(
    "INFRA_MCP_BASE_URL",
    "http://infra-mcp-server.infra-mcp.svc.cluster.local:8000",
)
GITHUB_API = "https://api.github.com"


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

class AppPlan(BaseModel):
    name: str
    description: str
    domain: str | None = None
    port: int = 8000
    has_database: bool = False
    env_secrets: dict[str, str] = {}
    stateless: bool = True

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _KEBAB_RE.match(v):
            raise ValueError("name must be lowercase kebab-case, 3–63 chars (e.g. my-app)")
        return v

    @field_validator("stateless")
    @classmethod
    def _validate_stateless(cls, v: bool) -> bool:
        if not v:
            raise ValueError("stateful app creation is not yet automated — set stateless=True")
        return v


class AppCreateResponse(BaseModel):
    task_id: int
    repo_url: str
    message: str


# ---------------------------------------------------------------------------
# GitHub repo creation
# ---------------------------------------------------------------------------

async def _create_github_repo(plan: AppPlan) -> str:
    """Create a new repo from app-template. Returns the HTML URL."""
    token = get_installation_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GITHUB_API}/repos/amerenda/app-template/generate",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "owner": "amerenda",
                "name": plan.name,
                "description": plan.description,
                "private": False,
                "include_all_branches": False,
            },
        )
    if resp.status_code == 422:
        detail = resp.json().get("message", resp.text[:200])
        raise HTTPException(status_code=409, detail=f"Repo already exists or name conflict: {detail}")
    if resp.status_code not in (200, 201):
        raise HTTPException(
            status_code=502,
            detail=f"GitHub repo creation failed ({resp.status_code}): {resp.text[:300]}",
        )
    return resp.json()["html_url"]


# ---------------------------------------------------------------------------
# Coder description builder
# ---------------------------------------------------------------------------

def _build_coder_description(plan: AppPlan) -> str:
    """Build a structured prompt for the coder agent to implement the new app."""
    domain = plan.domain or f"{plan.name}.amer.dev"
    uat_url = f"https://{plan.name}-uat.amer.dev"
    secrets_section = ""
    if plan.env_secrets:
        lines = "\n".join(f"  - {k} (BWS secret: {v})" for k, v in plan.env_secrets.items())
        secrets_section = f"\n\nEnvironment secrets needed:\n{lines}"

    return (
        f"App: {plan.name}\n"
        f"Repo: amerenda/{plan.name}\n"
        f"Branch: praetor-coder/initial-implementation\n"
        f"Purpose: {plan.description}\n"
        f"Port: {plan.port}\n"
        f"UAT URL: {uat_url}\n"
        f"Production URL: https://{domain}"
        f"{secrets_section}\n"
        f"\n"
        f"Initial implementation task:\n"
        f'1. Replace APP_NAME with "{plan.name}" in .github/workflows/build.yaml (3 occurrences)\n'
        f"2. Update the Dockerfile for a Python/FastAPI app (or the most appropriate language)\n"
        f"3. Implement the application: {plan.description}\n"
        f"4. Add a /healthz endpoint that returns {{\"status\": \"ok\"}}\n"
        f"5. Add requirements.txt (or go.mod / package.json as appropriate)\n"
        f'6. Open a PR titled "feat: initial implementation of {plan.name}"\n'
        f"\n"
        f"The repo was created from app-template and already has a CI/CD workflow skeleton.\n"
        f"Once the PR is merged, CI builds multi-arch Docker images and creates a deploy PR\n"
        f"on k3s-dean-gitops. ArgoCD auto-syncs UAT after the deploy PR merges.\n"
    )


# ---------------------------------------------------------------------------
# Background task: provision + dispatch
# ---------------------------------------------------------------------------

async def _provision_and_dispatch(plan: AppPlan, task_id: int, spec_toml: str | None = None) -> None:
    """Call infra-mcp to provision k3s manifests and CI runner, then dispatch coder.

    If spec_toml is provided (multi-feature new_app), dispatches feature_pipeline instead
    of a single coder run so each feature gets a clean context window.
    """
    async with httpx.AsyncClient(timeout=360) as client:
        try:
            resp = await client.post(
                f"{_INFRA_MCP_BASE}/app/create",
                json={
                    "name": plan.name,
                    "description": plan.description,
                    "domain": plan.domain,
                    "port": plan.port,
                    "has_database": plan.has_database,
                },
            )
            if resp.status_code != 200:
                logger.error(
                    "infra-mcp /app/create failed (%s) for %s: %s",
                    resp.status_code,
                    plan.name,
                    resp.text[:500],
                )
            else:
                result = resp.json()
                logger.info("infra-mcp /app/create succeeded for %s: %s", plan.name, result)
        except Exception as exc:
            logger.error("infra-mcp /app/create error for %s: %s", plan.name, exc)

    title = f"Initial implementation of {plan.name}"
    try:
        if spec_toml:
            description = f"```toml\n{spec_toml}\n```"
            dispatch_agent(task_id, title, description, "feature_pipeline")
            logger.info("feature_pipeline dispatched for task_id=%s app=%s", task_id, plan.name)
        else:
            description = _build_coder_description(plan)
            dispatch_agent(task_id, title, description, "code")
            logger.info("coder dispatched for task_id=%s app=%s", task_id, plan.name)
    except Exception as exc:
        logger.error("dispatch failed for task %s app=%s: %s", task_id, plan.name, exc)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.post(
    "/api/v1/app/create",
    response_model=AppCreateResponse,
    dependencies=[Depends(_check_auth)],
)
async def create_app(plan: AppPlan, background_tasks: BackgroundTasks) -> AppCreateResponse:
    """
    Full app pipeline: create GitHub repo, provision k3s manifests, dispatch coder agent.

    Synchronous steps (returns after these complete):
    - Validate AppPlan
    - Create GitHub repo from app-template via praetor-coder GitHub App

    Background steps (tracked via Hatchet at task_id):
    - scaffold_app + provision_app + open_deploy_pr (infra-mcp)
    - add_mac_mini_runner for CI (infra-mcp)
    - Dispatch coder agent to write initial implementation

    The praetor-coder GitHub App requires administration:write at the org level.
    """
    task_id = int(time.time())
    repo_url = await _create_github_repo(plan)
    logger.info("repo created: %s (task_id=%s)", repo_url, task_id)

    background_tasks.add_task(_provision_and_dispatch, plan, task_id)

    domain = plan.domain or f"{plan.name}.amer.dev"
    return AppCreateResponse(
        task_id=task_id,
        repo_url=repo_url,
        message=(
            f"Repo created at {repo_url}. "
            f"Provisioning k3s manifests (UAT: https://{plan.name}-uat.amer.dev, "
            f"Prod: https://{domain}) and CI runners in background. "
            f"Coder agent will start automatically once provisioning completes. "
            f"Track progress at https://hatchet.amer.dev (task_id={task_id})."
        ),
    )
