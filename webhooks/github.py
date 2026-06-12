"""FastAPI router: receives GitHub pull_request webhooks, dispatches Hatchet events."""
import hashlib
import hmac
import logging
import os

from fastapi import APIRouter, HTTPException, Request
from hatchet_sdk import Hatchet

logger = logging.getLogger(__name__)

router = APIRouter()
_hatchet: Hatchet | None = None


def _get_hatchet() -> Hatchet:
    global _hatchet
    if _hatchet is None:
        _hatchet = Hatchet()
    return _hatchet


def _verify_signature(body: bytes, header: str | None) -> None:
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        return
    if not header:
        raise HTTPException(status_code=401, detail="missing X-Hub-Signature-256")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header):
        raise HTTPException(status_code=401, detail="invalid signature")


@router.post("/webhooks/github")
async def github_webhook(request: Request) -> dict:
    body = await request.body()
    _verify_signature(body, request.headers.get("X-Hub-Signature-256"))

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return {"status": "pong"}
    if event != "pull_request":
        return {"status": "ignored", "event": event}

    payload = await request.json()
    action = payload.get("action", "")
    if action != "opened":
        return {"status": "ignored", "action": action}

    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {}).get("full_name", "")
    pr_number = pr.get("number")
    pr_url = pr.get("html_url", "")
    diff_url = pr.get("diff_url", "")
    author = pr.get("user", {}).get("login", "")

    _get_hatchet().client.event.push(
        "github:pr_opened",
        {
            "repo": repo,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "diff_url": diff_url,
            "author": author,
        },
        additional_metadata={"pr_url": pr_url},
    )
    logger.info("dispatched github:pr_opened for %s#%s", repo, pr_number)
    return {"status": "ok", "dispatched": "github:pr_opened"}
