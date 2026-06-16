"""E2E Phase 5: research agent — webhook → Hatchet → Mem0.

Flow:
  1. POST a signed Vikunja webhook with ai-research label to praetor adapter
  2. Adapter returns 200 and confirms dispatch of agent:research
  3. Poll Mem0 for a memory written under task-{id} namespace (up to 3 min)
  4. Assert memory content is non-empty
  5. Clean up Mem0 memory
"""
import json
import os

import httpx
import pytest

if not os.environ.get("E2E_TESTS"):
    pytest.skip("Set E2E_TESTS=1 to run e2e tests", allow_module_level=True)

from .conftest import (
    PRAETOR_BASE,
    MEM0_API_KEY,
    e2e_task_id,
    mem0_delete_by_agent,
    mem0_search,
    poll_until,
    vikunja_sig,
    vikunja_task_payload,
)

pytestmark = pytest.mark.e2e


class TestResearchAgentE2E:
    def test_research_webhook_dispatch(self):
        """Adapter accepts signed research webhook and confirms dispatch."""
        task_id = e2e_task_id()
        payload = vikunja_task_payload(
            task_id=task_id,
            title=f"E2E test {task_id}: summarise key k3s v1.30 changes",
            description="",
            label_ids=[14],  # ai-research
        )
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        sig = vikunja_sig(body)
        if sig:
            headers["X-Vikunja-Signature"] = sig

        resp = httpx.post(f"{PRAETOR_BASE}/webhooks/vikunja", content=body, headers=headers, timeout=10)
        assert resp.status_code == 200, f"adapter returned {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "agent:research" in data.get("dispatched", []), f"expected agent:research, got: {data}"

    def test_research_writes_to_mem0(self):
        """Full flow: webhook → agent runs → memory written to Mem0."""
        if not MEM0_API_KEY:
            pytest.skip("MEM0_API_KEY not available — check praetor-research-secrets in k8s")

        task_id = e2e_task_id()
        agent_id = f"task-{task_id}"

        # Pre-flight: namespace should be empty
        assert mem0_search("anything", agent_id) == []

        payload = vikunja_task_payload(
            task_id=task_id,
            title=f"E2E test {task_id}: summarise k3s networking defaults",
            description="Brief 3-sentence summary only — this is an automated test.",
            label_ids=[14],
        )
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        sig = vikunja_sig(body)
        if sig:
            headers["X-Vikunja-Signature"] = sig

        resp = httpx.post(f"{PRAETOR_BASE}/webhooks/vikunja", content=body, headers=headers, timeout=10)
        assert resp.status_code == 200

        try:
            results = poll_until(
                lambda: mem0_search("k3s networking", agent_id),
                timeout=360,
                interval=10,
                fail_msg=f"research agent did not write to mem0 namespace task-{task_id} within 6 minutes",
            )
            assert results, "search returned empty after poll succeeded"
            assert any(r.get("memory") for r in results), f"memory field empty in results: {results}"
        finally:
            mem0_delete_by_agent(agent_id)
