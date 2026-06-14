"""Shared fixtures and helpers for E2E tests against the live cluster."""
import base64
import hashlib
import hmac
import subprocess
import time

import httpx
import pytest

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Secret helpers
# ---------------------------------------------------------------------------

def _k8s_secret(namespace: str, secret: str, key: str) -> str:
    try:
        out = subprocess.check_output(
            ["kubectl", "get", "secret", "-n", namespace, secret, "-o", f"jsonpath={{.data.{key}}}"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return base64.b64decode(out).decode().strip()
    except Exception:
        return ""


VIKUNJA_WEBHOOK_SECRET = _k8s_secret("praetor", "praetor-webhook-secrets", "vikunja-webhook-secret")
GITHUB_WEBHOOK_SECRET = _k8s_secret("praetor", "praetor-webhook-secrets", "github-webhook-secret")
MEM0_API_KEY = _k8s_secret("praetor", "praetor-research-secrets", "mem0-api-key")
LANGFUSE_PUBLIC_KEY = _k8s_secret("praetor", "praetor-research-secrets", "langfuse-public-key")
LANGFUSE_SECRET_KEY = _k8s_secret("praetor", "praetor-research-secrets", "langfuse-secret-key")

PRAETOR_BASE = "https://praetor.amer.dev"
MEM0_BASE = "https://mem0.amer.dev"
LANGFUSE_BASE = "https://langfuse.amer.dev"


# ---------------------------------------------------------------------------
# HMAC signature helpers
# ---------------------------------------------------------------------------

def vikunja_sig(body: bytes) -> str:
    if not VIKUNJA_WEBHOOK_SECRET:
        return ""
    return "sha256=" + hmac.new(VIKUNJA_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def github_sig(body: bytes) -> str:
    if not GITHUB_WEBHOOK_SECRET:
        return ""
    return "sha256=" + hmac.new(GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Polling helper
# ---------------------------------------------------------------------------

def poll_until(fn, timeout: int = 180, interval: int = 10, fail_msg: str = "timed out") -> object:
    """Call fn() every interval seconds until it returns a truthy value or timeout."""
    deadline = time.monotonic() + timeout
    last_exc = None
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result:
                return result
        except Exception as exc:
            last_exc = exc
        time.sleep(interval)
    if last_exc:
        pytest.fail(f"{fail_msg}: last error: {last_exc}")
    pytest.fail(fail_msg)


# ---------------------------------------------------------------------------
# Mem0 helpers
# ---------------------------------------------------------------------------

def mem0_search(query: str, agent_id: str) -> list[dict]:
    if not MEM0_API_KEY:
        return []
    resp = httpx.post(
        f"{MEM0_BASE}/search",
        json={"query": query, "agent_id": agent_id},
        headers={"x-api-key": MEM0_API_KEY, "Content-Type": "application/json"},
        timeout=15,
    )
    if resp.status_code != 200:
        return []
    return resp.json().get("results", [])


def mem0_delete_by_agent(agent_id: str) -> None:
    """Best-effort cleanup: delete all memories for an agent_id."""
    if not MEM0_API_KEY:
        return
    resp = httpx.get(
        f"{MEM0_BASE}/memories",
        params={"agent_id": agent_id},
        headers={"x-api-key": MEM0_API_KEY},
        timeout=15,
    )
    if resp.status_code != 200:
        return
    for m in resp.json().get("results", []):
        mid = m.get("id")
        if mid:
            httpx.delete(
                f"{MEM0_BASE}/memories/{mid}",
                headers={"x-api-key": MEM0_API_KEY},
                timeout=10,
            )


# ---------------------------------------------------------------------------
# Unique test task IDs
# Test IDs live in the 90000-99999 range to avoid clashing with real Vikunja tasks.
# ---------------------------------------------------------------------------

def e2e_task_id() -> int:
    return 90000 + int(time.time()) % 9999


# ---------------------------------------------------------------------------
# Webhook payload factories
# ---------------------------------------------------------------------------

def vikunja_task_payload(task_id: int, title: str, description: str, label_ids: list[int]) -> dict:
    return {
        "event_type": "task.updated",
        "data": {
            "task": {
                "id": task_id,
                "title": title,
                "description": description,
                "labels": [{"id": lid} for lid in label_ids],
            }
        },
    }


def github_pr_payload(pr_number: int, repo: str = "amerenda/praetor") -> dict:
    return {
        "action": "opened",
        "pull_request": {
            "number": pr_number,
            "html_url": f"https://github.com/{repo}/pull/{pr_number}",
            "diff_url": f"https://github.com/{repo}/pull/{pr_number}.diff",
            "user": {"login": "e2e-tester"},
        },
        "repository": {"full_name": repo},
    }
