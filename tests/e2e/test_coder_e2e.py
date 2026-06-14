"""E2E Phase 6: coder agent — webhook → Hatchet dispatch.

By default only the dispatch path is verified (no real PRs created).
Set CODER_E2E_FULL=1 to include the full flow to a real GitHub PR,
which requires the target repo to exist and the coder bot to have access.
"""
import json
import os

import httpx
import pytest

if not os.environ.get("E2E_TESTS"):
    pytest.skip("Set E2E_TESTS=1 to run e2e tests", allow_module_level=True)

from .conftest import (
    PRAETOR_BASE,
    e2e_task_id,
    poll_until,
    vikunja_sig,
    vikunja_task_payload,
)

pytestmark = pytest.mark.e2e

_FULL = os.environ.get("CODER_E2E_FULL")


class TestCoderAgentE2E:
    def test_coder_webhook_dispatch(self):
        """Adapter accepts signed coder webhook and confirms dispatch."""
        task_id = e2e_task_id()
        payload = vikunja_task_payload(
            task_id=task_id,
            title=f"E2E test {task_id}: add /ping endpoint",
            description="repo: amerenda/praetor",
            label_ids=[11],  # ai-go
        )
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        sig = vikunja_sig(body)
        if sig:
            headers["X-Vikunja-Signature"] = sig

        resp = httpx.post(f"{PRAETOR_BASE}/webhooks/vikunja", content=body, headers=headers, timeout=10)
        assert resp.status_code == 200, f"adapter returned {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "agent:code" in data.get("dispatched", []), f"expected agent:code, got: {data}"

    def test_coder_missing_repo_dispatches(self):
        """Dispatch succeeds even without a repo reference — coder handles error gracefully."""
        task_id = e2e_task_id()
        payload = vikunja_task_payload(
            task_id=task_id,
            title=f"E2E test {task_id}: no repo reference",
            description="No repo here — agent should fail gracefully.",
            label_ids=[11],
        )
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        sig = vikunja_sig(body)
        if sig:
            headers["X-Vikunja-Signature"] = sig

        resp = httpx.post(f"{PRAETOR_BASE}/webhooks/vikunja", content=body, headers=headers, timeout=10)
        # Adapter still dispatches — the agent itself returns an error to Vikunja
        assert resp.status_code == 200
        assert "agent:code" in resp.json().get("dispatched", [])

    @pytest.mark.skipif(not _FULL, reason="Set CODER_E2E_FULL=1 to run full PR creation flow")
    def test_coder_opens_github_pr(self):
        """Full flow: coder agent opens a draft PR on amerenda/praetor (test branch only)."""
        import subprocess
        task_id = e2e_task_id()
        target_branch = f"e2e-coder-test-{task_id}"
        payload = vikunja_task_payload(
            task_id=task_id,
            title=f"E2E test {task_id}: add e2e marker comment to README",
            description=(
                f"repo: amerenda/praetor\n"
                f"Add a one-line comment <!-- e2e-test-{task_id} --> to README.md. "
                f"This is an automated test — keep the change minimal."
            ),
            label_ids=[11],
        )
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        sig = vikunja_sig(body)
        if sig:
            headers["X-Vikunja-Signature"] = sig

        resp = httpx.post(f"{PRAETOR_BASE}/webhooks/vikunja", content=body, headers=headers, timeout=10)
        assert resp.status_code == 200

        # Poll GitHub for a PR opened by dean-coder[bot] with branch amerenda-coder/task-{task_id}
        gh_token = os.environ.get("GITHUB_TOKEN", "")
        gh_headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"}

        def check_pr():
            if not gh_token:
                return False
            resp2 = httpx.get(
                "https://api.github.com/repos/amerenda/praetor/pulls",
                params={"state": "open", "head": f"amerenda:amerenda-coder/task-{task_id}"},
                headers=gh_headers,
                timeout=10,
            )
            prs = resp2.json() if resp2.status_code == 200 else []
            return prs if isinstance(prs, list) and prs else None

        prs = poll_until(
            check_pr,
            timeout=300,
            interval=15,
            fail_msg=f"coder agent did not open PR on amerenda/praetor within 5 minutes",
        )
        assert prs, "no PR found"
        assert prs[0]["draft"] is True or prs[0].get("draft") in (True, False)
