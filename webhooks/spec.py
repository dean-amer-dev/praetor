"""FastAPI router: spec layer — parse approved TOML spec and dispatch the right agent."""
from __future__ import annotations

import logging
import os
import time
import tomllib

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from common.dispatch import EVENT_MAP, dispatch_agent
from common.memory_tools import add_memory, delete_memory, search_memory

logger = logging.getLogger(__name__)
router = APIRouter()
_bearer = HTTPBearer(auto_error=False)


def _check_auth(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    expected = os.environ.get("PRAETOR_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=500, detail="PRAETOR_API_KEY not configured on server")
    token = creds.credentials if creds else ""
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


class SpecExecuteRequest(BaseModel):
    spec_toml: str


class SpecExecuteResponse(BaseModel):
    task_id: int
    event: str
    hatchet_url: str
    routing: str  # "app_factory" | "dispatch"


class MemorySearchRequest(BaseModel):
    query: str


class MemorySearchResponse(BaseModel):
    results: list[str]


class MemoryAddRequest(BaseModel):
    content: str


class MemoryAddResponse(BaseModel):
    stored: bool = True


class MemoryDeleteRequest(BaseModel):
    query: str


class MemoryDeleteResponse(BaseModel):
    deleted: int


@router.post(
    "/api/v1/spec/execute",
    response_model=SpecExecuteResponse,
    dependencies=[Depends(_check_auth)],
)
async def execute_spec(req: SpecExecuteRequest) -> SpecExecuteResponse:
    """
    Parse an approved TOML spec and dispatch the appropriate agent.

    Routes:
    - new_app spec with "app_factory" in agents → calls create_app handler
    - modify_app / fix_pr → dispatches agent:code directly
    """
    try:
        spec = tomllib.loads(req.spec_toml)
    except tomllib.TOMLDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid TOML: {exc}")

    task_section = spec.get("task", {})
    task_type = task_section.get("type", "")
    title = task_section.get("title", "Untitled task")
    agents = spec.get("dispatch", {}).get("agents", [])

    if task_type not in ("new_app", "modify_app", "fix_pr"):
        raise HTTPException(
            status_code=422,
            detail=f"spec task.type must be new_app, modify_app, or fix_pr — got '{task_type}'",
        )

    task_id = int(time.time())

    if task_type == "new_app" and "app_factory" in agents:
        return await _route_new_app(spec, title, task_id, req.spec_toml)
    else:
        return await _route_dispatch(spec, task_type, title, task_id, req.spec_toml)


async def _route_new_app(spec: dict, title: str, task_id: int, raw_toml: str) -> SpecExecuteResponse:
    """Route new_app specs to the app_factory handler."""
    from .app_factory import AppPlan

    repos = spec.get("repos", {})
    infra = spec.get("infra", {})
    app = spec.get("app", {})
    task = spec.get("task", {})
    dispatch_cfg = spec.get("dispatch", {})

    name = repos.get("primary", "").split("/")[-1]
    if not name:
        raise HTTPException(status_code=422, detail="spec repos.primary must contain the app name")

    features = app.get("features", [])
    framework = app.get("framework", "")
    port = infra.get("port", 8000)
    hostname = infra.get("hostname", f"{name}.amer.dev")
    request_limit = dispatch_cfg.get("request_limit", 80)

    description_lines = [task.get("description", "")]
    if framework:
        description_lines.append(f"Stack: {framework}")
    if features:
        description_lines.append("Features:")
        description_lines.extend(f"  - {f}" for f in features)
    description_lines.append(f"request_limit: {request_limit}")

    from .app_factory import _create_github_repo, _provision_and_dispatch
    import asyncio

    plan = AppPlan(
        name=name,
        description="\n".join(description_lines),
        domain=hostname,
        port=port,
    )

    try:
        repo_url = await _create_github_repo(plan)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"repo creation failed: {exc}")

    # Pass raw_toml so _provision_and_dispatch can use feature_pipeline for multi-feature specs
    asyncio.ensure_future(_provision_and_dispatch(plan, task_id, raw_toml if len(features) > 1 else None))
    logger.info("spec new_app routed to app_factory: name=%s task_id=%s features=%d", name, task_id, len(features))

    event = "pipeline:feature_decompose" if len(features) > 1 else "agent:code"
    return SpecExecuteResponse(
        task_id=task_id,
        event=event,
        hatchet_url="https://hatchet.amer.dev",
        routing="app_factory",
    )


async def _route_dispatch(
    spec: dict, task_type: str, title: str, task_id: int, raw_toml: str
) -> SpecExecuteResponse:
    """Route modify_app and fix_pr specs. Multi-feature modify_app uses feature_pipeline."""
    description = f"```toml\n{raw_toml}\n```"

    app = spec.get("app", {})
    features = app.get("features") or app.get("changes") or []

    if task_type == "modify_app" and len(features) > 1:
        agent_type = "feature_pipeline"
    else:
        agent_type = "code"

    events = dispatch_agent(task_id, title, description, agent_type)
    event = events[0] if events else EVENT_MAP.get(agent_type, "agent:code")
    logger.info("spec %s dispatched as %s task_id=%s features=%d", task_type, event, task_id, len(features))

    return SpecExecuteResponse(
        task_id=task_id,
        event=event,
        hatchet_url="https://hatchet.amer.dev",
        routing="dispatch",
    )


@router.post(
    "/api/v1/memory/search",
    response_model=MemorySearchResponse,
    dependencies=[Depends(_check_auth)],
)
async def memory_search(req: MemorySearchRequest) -> MemorySearchResponse:
    """Search the planner-global Mem0 namespace for context relevant to a query."""
    results = await search_memory(req.query, "planner-global")
    return MemorySearchResponse(results=results)


@router.post(
    "/api/v1/memory/add",
    response_model=MemoryAddResponse,
    dependencies=[Depends(_check_auth)],
)
async def memory_add(req: MemoryAddRequest) -> MemoryAddResponse:
    """Add content to the planner-global Mem0 namespace."""
    await add_memory(req.content, "planner-global")
    return MemoryAddResponse()


@router.post(
    "/api/v1/memory/delete",
    response_model=MemoryDeleteResponse,
    dependencies=[Depends(_check_auth)],
)
async def memory_delete_endpoint(req: MemoryDeleteRequest) -> MemoryDeleteResponse:
    """Delete memories matching a query from the planner-global Mem0 namespace."""
    deleted = await delete_memory(req.query, "planner-global")
    return MemoryDeleteResponse(deleted=deleted)
