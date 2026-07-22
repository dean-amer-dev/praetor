"""FastAPI router: receives GitHub pull_request webhooks, dispatches Hatchet events."""
import hashlib
import hmac
import logging
import os

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
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


def _dispatch_pr_event(repo: str, pr_number: str, pr_url: str, diff_url: str, author: str, head_branch: str = "", attempt: int = 0) -> None:
    _get_hatchet().event.push(
        "github:pr_opened",
        {
            "repo": repo,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "diff_url": diff_url,
            "author": author,
            "head_branch": head_branch,
            "attempt": attempt,
        },
        additional_metadata={"pr_url": pr_url},
    )
    logger.info("dispatched github:pr_opened for %s#%s", repo, pr_number)


# Auto-review-on-PR disabled: pr-reviewer runs were piling up on the shared single-session
# murderbot backend faster than Hatchet's step timeout would clear them, and a cancelled
# Hatchet step does not abort the in-flight LiteLLM/vLLM request — the orphaned generation
# just keeps running server-side, permanently occupying the one execution slot. Re-enable
# only once cancellation actually propagates to an aborted upstream HTTP request.
_PR_REVIEW_ENABLED = False


@router.post("/webhooks/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    body = await request.body()
    _verify_signature(body, request.headers.get("X-Hub-Signature-256"))

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return {"status": "pong"}
    if event != "pull_request":
        return {"status": "ignored", "event": event}
    if not _PR_REVIEW_ENABLED:
        return {"status": "disabled"}

    payload = await request.json()
    action = payload.get("action", "")

    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {}).get("full_name", "")
    pr_number = str(pr.get("number", ""))
    pr_url = pr.get("html_url", "")
    diff_url = pr.get("diff_url", "")
    author = pr.get("user", {}).get("login", "")
    head_branch = pr.get("head", {}).get("ref", "")

    if action == "opened":
        attempt = 0
    elif action == "synchronize" and head_branch.startswith("praetor-coder/"):
        attempt = 1
    else:
        return {"status": "ignored", "action": action}

    # Push to Hatchet in the background so GitHub gets a fast 200 and stops retrying.
    background_tasks.add_task(_dispatch_pr_event, repo, pr_number, pr_url, diff_url, author, head_branch, attempt)
    return {"status": "ok", "dispatched": "github:pr_opened", "attempt": attempt}
