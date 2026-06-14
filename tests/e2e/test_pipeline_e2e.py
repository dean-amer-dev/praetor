"""E2E Phase 8: multi-agent pipeline — both labels → research_then_code DAG.

Flow:
  1. POST signed webhook with both ai-research (14) + ai-go (11) labels
  2. Adapter dispatches pipeline:research_code (NOT individual events)
  3. Poll Mem0 for research output written by the pipeline's research step
  4. Clean up Mem0 memory
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


class TestPipelineE2E:
    def test_pipeline_webhook_dispatch(self):
        """Both labels → adapter dispatches pipeline:research_code, not individual events."""
        task_id = e2e_task_id()
        payload = vikunja_task_payload(
            task_id=task_id,
            title=f"E2E test {task_id}: pipeline dispatch check",
            description="repo: amerenda/praetor",
            label_ids=[14, 11],  # both ai-research + ai-go
        )
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        sig = vikunja_sig(body)
        if sig:
            headers["X-Vikunja-Signature"] = sig

        resp = httpx.post(f"{PRAETOR_BASE}/webhooks/vikunja", content=body, headers=headers, timeout=10)
        assert resp.status_code == 200, f"adapter returned {resp.status_code}: {resp.text}"
        data = resp.json()
        dispatched = data.get("dispatched", [])
        assert dispatched == ["pipeline:research_code"], (
            f"expected ['pipeline:research_code'], got {dispatched} — "
            "adapter must not dispatch individual agent:research / agent:code events when both labels are present"
        )

    def test_pipeline_research_step_writes_mem0(self):
        """Pipeline research step writes findings to task-scoped Mem0 namespace."""
        if not MEM0_API_KEY:
            pytest.skip("MEM0_API_KEY not available — check praetor-research-secrets in k8s")

        task_id = e2e_task_id()
        agent_id = f"task-{task_id}"

        payload = vikunja_task_payload(
            task_id=task_id,
            title=f"E2E test {task_id}: pipeline research step",
            description=(
                "repo: amerenda/praetor\n"
                "Summarise the three main purposes of praetor in one sentence. "
                "This is an automated test — keep the research brief."
            ),
            label_ids=[14, 11],
        )
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        sig = vikunja_sig(body)
        if sig:
            headers["X-Vikunja-Signature"] = sig

        resp = httpx.post(f"{PRAETOR_BASE}/webhooks/vikunja", content=body, headers=headers, timeout=10)
        assert resp.status_code == 200
        assert resp.json().get("dispatched") == ["pipeline:research_code"]

        try:
            results = poll_until(
                lambda: mem0_search("praetor", agent_id),
                timeout=180,
                interval=10,
                fail_msg=f"pipeline research step did not write to mem0 task-{task_id} within 3 minutes",
            )
            assert results
            assert any(r.get("memory") for r in results), f"memory field empty: {results}"
        finally:
            mem0_delete_by_agent(agent_id)
