"""E2E Phase 9: Langfuse observability — research run → trace in Langfuse.

Flow:
  1. Trigger a research agent run via the webhook adapter
  2. Wait for the Mem0 write to confirm the agent completed
  3. Query Langfuse API for a trace tagged with the task ID
  4. Assert trace exists with at least one span
"""
import base64
import json
import os
import time

import httpx
import pytest

if not os.environ.get("E2E_TESTS"):
    pytest.skip("Set E2E_TESTS=1 to run e2e tests", allow_module_level=True)

from .conftest import (
    PRAETOR_BASE,
    LANGFUSE_BASE,
    LANGFUSE_PUBLIC_KEY,
    LANGFUSE_SECRET_KEY,
    MEM0_API_KEY,
    e2e_task_id,
    mem0_delete_by_agent,
    mem0_search,
    poll_until,
    vikunja_sig,
    vikunja_task_payload,
)

pytestmark = pytest.mark.e2e


def _langfuse_auth() -> str:
    """Basic auth header for Langfuse API."""
    if not LANGFUSE_PUBLIC_KEY or not LANGFUSE_SECRET_KEY:
        return ""
    creds = base64.b64encode(f"{LANGFUSE_PUBLIC_KEY}:{LANGFUSE_SECRET_KEY}".encode()).decode()
    return f"Basic {creds}"


class TestLangfuseE2E:
    def test_langfuse_ui_accessible(self):
        """Langfuse UI is reachable."""
        resp = httpx.get(LANGFUSE_BASE, timeout=10, follow_redirects=True)
        assert resp.status_code == 200

    def test_langfuse_api_reachable(self):
        """Langfuse public API is reachable with project credentials."""
        auth = _langfuse_auth()
        if not auth:
            pytest.skip("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not in praetor-research-secrets")
        resp = httpx.get(
            f"{LANGFUSE_BASE}/api/public/projects",
            headers={"Authorization": auth},
            timeout=10,
        )
        assert resp.status_code in (200, 404), f"unexpected status {resp.status_code}: {resp.text[:100]}"

    def test_research_run_produces_langfuse_trace(self):
        """Research agent run is traced in Langfuse with correct task tag."""
        auth = _langfuse_auth()
        if not auth:
            pytest.skip("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not in praetor-research-secrets")
        if not MEM0_API_KEY:
            pytest.skip("MEM0_API_KEY not available — need it to confirm agent completed")

        task_id = e2e_task_id()
        agent_id = f"task-{task_id}"

        payload = vikunja_task_payload(
            task_id=task_id,
            title=f"E2E test {task_id}: Langfuse trace check",
            description="One sentence summary of what Hatchet does. Automated test.",
            label_ids=[14],
        )
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        sig = vikunja_sig(body)
        if sig:
            headers["X-Vikunja-Signature"] = sig

        resp = httpx.post(f"{PRAETOR_BASE}/webhooks/vikunja", content=body, headers=headers, timeout=10)
        assert resp.status_code == 200

        # Step 1: wait for Mem0 to confirm agent completed
        try:
            poll_until(
                lambda: mem0_search("Hatchet", agent_id),
                timeout=180,
                interval=10,
                fail_msg=f"research agent did not write to mem0 task-{task_id}",
            )
        finally:
            mem0_delete_by_agent(agent_id)

        # Step 2: give Langfuse a moment to flush the trace (SDK batches)
        time.sleep(5)

        # Step 3: query Langfuse for traces tagged with the task ID
        tag = f"task-{task_id}"

        def check_trace():
            r = httpx.get(
                f"{LANGFUSE_BASE}/api/public/traces",
                params={"tags": tag, "limit": 5},
                headers={"Authorization": auth},
                timeout=15,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            traces = data.get("data", [])
            return traces if traces else None

        traces = poll_until(
            check_trace,
            timeout=60,
            interval=5,
            fail_msg=f"no Langfuse trace found with tag task-{task_id} within 60s after agent completion",
        )
        assert traces, "trace list is empty"
        trace = traces[0]
        assert trace.get("name", "").startswith("research-task-"), f"unexpected trace name: {trace.get('name')}"
