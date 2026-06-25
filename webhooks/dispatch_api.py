"""FastAPI router: direct dispatch endpoint — any interface can trigger agents without Vikunja."""
from __future__ import annotations

import logging
import os
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from common.dispatch import AgentType, dispatch_agent

logger = logging.getLogger(__name__)

router = APIRouter()
_bearer = HTTPBearer(auto_error=False)


async def _is_coder_busy() -> bool:
    """Return True if a coder task is currently queued or running in Hatchet."""
    base = os.environ.get("HATCHET_API_BASE_URL", "").rstrip("/")
    token = os.environ.get("HATCHET_CLIENT_TOKEN", "")
    if not base or not token:
        return False  # can't check — allow dispatch
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{base}/task-stats",
                params={"taskNames": "coder"},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            stats = resp.json().get("coder", {})
            queued = stats.get("queued", {}).get("total", 0)
            running = stats.get("running", {}).get("total", 0)
            return (queued + running) > 0
    except Exception as exc:
        logger.warning("coder busy check failed: %s", exc)
        return False  # fail open — don't block dispatches on API errors


def _check_auth(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    expected = os.environ.get("PRAETOR_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=500, detail="PRAETOR_API_KEY not configured on server")
    token = creds.credentials if creds else ""
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


class DispatchRequest(BaseModel):
    title: str
    description: str = ""
    type: AgentType
    create_vikunja_task: bool = False


class DispatchResponse(BaseModel):
    task_id: int
    event: str
    hatchet_url: str
    vikunja_task_id: int | None = None
    vikunja_task_url: str | None = None


class StatusResponse(BaseModel):
    task_id: int
    done: bool
    mem0_summary: str | None = None
    vikunja_task_id: int | None = None
    vikunja_task_url: str | None = None


@router.post("/api/v1/dispatch", response_model=DispatchResponse, dependencies=[Depends(_check_auth)])
async def dispatch(req: DispatchRequest) -> DispatchResponse:
    if req.type == "code" and await _is_coder_busy():
        raise HTTPException(
            status_code=423,
            detail="coder is busy — a task is already queued or running. Retry when idle.",
        )

    task_id = int(time.time())
    vikunja_task_id: int | None = None
    vikunja_task_url: str | None = None

    if req.create_vikunja_task:
        vikunja_task_id, vikunja_task_url = await _create_vikunja_task(req.title, req.description)
        if vikunja_task_id is not None:
            task_id = vikunja_task_id

    events = dispatch_agent(task_id, req.title, req.description, req.type)
    event = events[0]
    logger.info("dispatch api: %s for task_id=%s", event, task_id)

    return DispatchResponse(
        task_id=task_id,
        event=event,
        hatchet_url="https://hatchet.amer.dev",
        vikunja_task_id=vikunja_task_id,
        vikunja_task_url=vikunja_task_url,
    )


@router.get("/api/v1/status/{task_id}", response_model=StatusResponse, dependencies=[Depends(_check_auth)])
async def status(task_id: int) -> StatusResponse:
    mem0_summary = await _poll_mem0(task_id)
    done = mem0_summary is not None
    return StatusResponse(task_id=task_id, done=done, mem0_summary=mem0_summary)


async def _poll_mem0(task_id: int) -> str | None:
    base = os.environ.get("MEM0_BASE_URL", "").rstrip("/")
    key = os.environ.get("MEM0_API_KEY", "")
    if not base or not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{base}/memories",
                params={"agent_id": f"task-{task_id}"},
                headers={"x-api-key": key},
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if results:
                return results[0].get("memory")
    except Exception as exc:
        logger.warning("mem0 poll failed for task %s: %s", task_id, exc)
    return None


async def _create_vikunja_task(title: str, description: str) -> tuple[int | None, str | None]:
    base = os.environ.get("VIKUNJA_BASE_URL", "https://todo.amer.dev")
    token = os.environ.get("VIKUNJA_TOKEN", "")
    project_id = int(os.environ.get("VIKUNJA_PROJECT_ID", "21"))
    if not token:
        logger.warning("VIKUNJA_TOKEN not set — cannot create Vikunja task")
        return None, None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.put(
                f"{base}/api/v1/projects/{project_id}/tasks",
                json={"title": title, "description": description},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            task_id = data.get("id")
            url = f"{base}/tasks/{task_id}" if task_id else None
            return task_id, url
    except Exception as exc:
        logger.warning("Vikunja task creation failed: %s", exc)
        return None, None
