"""FastAPI router: receives Vikunja task.updated webhooks, dispatches Hatchet events."""
import hashlib
import hmac
import json as _json
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Request

from common.dispatch import dispatch_agent

logger = logging.getLogger(__name__)

router = APIRouter()

# Label IDs from Vikunja (CLAUDE.md reference)
LABEL_RESEARCH = 14
LABEL_GO = 11
LABEL_PLAN_ONLY = 13


def _verify_signature(body: bytes, header: str | None) -> None:
    secret = os.environ.get("VIKUNJA_WEBHOOK_SECRET", "")
    if not secret or not header:
        # No secret configured (dev mode), or Vikunja didn't send a signature
        # (Vikunja does not implement HMAC signing despite accepting a secret field).
        return
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header):
        raise HTTPException(status_code=401, detail="invalid signature")


@router.post("/webhooks/vikunja")
async def vikunja_webhook(request: Request) -> dict:
    body = await request.body()
    _verify_signature(body, request.headers.get("X-Vikunja-Signature"))

    try:
        payload = await request.json()
    except _json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    event_type = payload.get("event_type", "")

    if event_type not in ("task.updated", "task.created"):
        return {"status": "ignored", "event_type": event_type}

    task = payload.get("data", {}).get("task", {})
    task_id = task.get("id")
    if not task_id:
        return {"status": "no task id"}

    labels = task.get("labels") or []
    label_ids = {lbl["id"] for lbl in labels if isinstance(lbl, dict)}

    dispatched: list[str] = []
    title = task.get("title", "")
    description = task.get("description", "")
    meta = {"vikunja_task_id": str(task_id)}

    if LABEL_RESEARCH in label_ids and LABEL_GO in label_ids:
        dispatched = dispatch_agent(task_id, title, description, "pipeline", meta)
        logger.info("dispatched pipeline:research_code for task %s", task_id)
    else:
        if LABEL_RESEARCH in label_ids:
            dispatched += dispatch_agent(task_id, title, description, "research", meta)
            logger.info("dispatched agent:research for task %s", task_id)

        if LABEL_GO in label_ids:
            dispatched += dispatch_agent(task_id, title, description, "code", meta)
            logger.info("dispatched agent:code for task %s", task_id)

    return {"status": "ok", "dispatched": dispatched}


async def register_webhook_on_startup() -> None:
    """Idempotently register this adapter's webhook with Vikunja on startup."""
    base = os.environ.get("VIKUNJA_BASE_URL", "https://todo.amer.dev")
    token = os.environ.get("VIKUNJA_TOKEN", "")
    project_id = int(os.environ.get("VIKUNJA_PROJECT_ID", "21"))
    target_url = os.environ.get("WEBHOOK_TARGET_URL", "")
    secret = os.environ.get("VIKUNJA_WEBHOOK_SECRET", "")

    if not token or not target_url:
        logger.warning("VIKUNJA_TOKEN or WEBHOOK_TARGET_URL not set — skipping webhook registration")
        return

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{base}/api/v1/projects/{project_id}/webhooks", headers=headers)
            if resp.status_code == 401:
                logger.error(
                    "Vikunja token is invalid/expired (401) — webhook not registered. "
                    "Update vikjuna-api-key-full-access in BWS with a fresh token."
                )
                return
            if resp.status_code == 404:
                logger.warning("project %s not found — webhook registration skipped", project_id)
                return
            resp.raise_for_status()
            existing = resp.json() or []

            for wh in existing:
                if wh.get("target_url") == target_url:
                    logger.info("Vikunja webhook already registered (id=%s)", wh.get("id"))
                    return

            body = {
                "target_url": target_url,
                "events": ["task.updated", "task.created"],
                "secret": secret,
            }
            create_resp = await client.put(
                f"{base}/api/v1/projects/{project_id}/webhooks",
                json=body,
                headers=headers,
            )
            create_resp.raise_for_status()
            wh_id = create_resp.json().get("id")
            logger.info("registered Vikunja webhook id=%s for project %s", wh_id, project_id)
    except httpx.HTTPStatusError as exc:
        logger.error("webhook registration failed (%s) — adapter will still handle requests", exc)
