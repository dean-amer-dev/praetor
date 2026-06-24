"""Unit tests for Phase 21 — coder re-dispatch loop."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pr_payload(action: str, head_ref: str = "", repo: str = "amerenda/praetor", pr_number: int = 99) -> dict:
    return {
        "action": action,
        "pull_request": {
            "number": pr_number,
            "html_url": f"https://github.com/{repo}/pull/{pr_number}",
            "diff_url": f"https://github.com/{repo}/pull/{pr_number}.diff",
            "user": {"login": "praetor-coder[bot]"},
            "head": {"ref": head_ref},
        },
        "repository": {"full_name": repo},
    }


def _post(client, payload: dict, event: str = "pull_request") -> "httpx.Response":
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={"Content-Type": "application/json", "X-GitHub-Event": event},
    )


@pytest.fixture
def web_client(mock_hatchet):
    with patch("webhooks.vikunja.register_webhook_on_startup", new=AsyncMock()):
        from webhooks.app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c, mock_hatchet


# ---------------------------------------------------------------------------
# 1 & 2. Coder worker — Mode A / Mode B prompt building
# ---------------------------------------------------------------------------

class TestCoderWorkerPrompt:
    async def _run(self, description: str, task_id: int = 5):
        from agents.coder.worker import CoderInput, _run_coder

        input_obj = CoderInput(task_id=task_id, task_title="Test task", task_description=description)
        mock_ctx = MagicMock()
        mock_result = MagicMock()
        mock_result.output = "done"
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=mock_result)

        with (
            patch("agents.coder.worker.build_agent", return_value=mock_agent),
            patch("agents.coder.worker.assemble_prompt", AsyncMock(return_value="PROMPT")),
            patch("agents.coder.worker.search_memory", AsyncMock(return_value=[])),
            patch("agents.coder.worker.add_memory", AsyncMock()),
            patch("agents.coder.worker.update_vikunja_task", AsyncMock()),
        ):
            await _run_coder(input_obj, mock_ctx)

        return mock_agent.run.call_args[0][0]  # the prompt string

    async def test_mode_b_prompt_contains_revision_info(self, scratch_dir):
        description = (
            "repo: amerenda/praetor\n"
            "pr: 42\n"
            "branch: praetor-coder/task-1\n"
            "attempt: 1\n"
            "Fix review feedback."
        )
        prompt = await self._run(description, task_id=5)
        assert "revision attempt 2/2" in prompt
        assert "PR #42" in prompt
        assert "praetor-coder/task-1" in prompt

    async def test_mode_b_prompt_no_create_branch(self, scratch_dir):
        description = (
            "repo: amerenda/praetor\n"
            "pr: 42\n"
            "branch: praetor-coder/task-1\n"
            "attempt: 1\n"
            "Fix review feedback."
        )
        prompt = await self._run(description, task_id=5)
        assert "Create branch" not in prompt
        assert "open a draft PR" not in prompt

    async def test_mode_a_prompt_creates_branch(self, scratch_dir):
        description = "repo: amerenda/praetor\nBuild the feature."
        prompt = await self._run(description, task_id=7)
        assert "Create branch praetor-coder/task-7" in prompt


# ---------------------------------------------------------------------------
# 3–5. Reviewer worker — re-dispatch logic
# ---------------------------------------------------------------------------

class TestReviewerRedispatch:
    def _make_input(self, attempt: int = 0, head_branch: str = "praetor-coder/task-1"):
        from agents.pr_reviewer.worker import PROpenedInput
        return PROpenedInput(
            repo="amerenda/praetor",
            pr_number="42",
            pr_url="https://github.com/amerenda/praetor/pull/42",
            head_branch=head_branch,
            attempt=attempt,
        )

    async def _run(self, input_obj, output_text: str):
        from agents.pr_reviewer.worker import _run_reviewer

        mock_ctx = MagicMock()
        mock_result = MagicMock()
        mock_result.output = output_text
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=mock_result)

        with (
            patch("agents.pr_reviewer.worker.build_agent", return_value=mock_agent),
            patch("agents.pr_reviewer.worker.assemble_prompt", AsyncMock(return_value="PROMPT")),
            patch("agents.pr_reviewer.worker.dispatch_agent") as mock_dispatch,
            patch("agents.pr_reviewer.worker.add_memory", AsyncMock()),
        ):
            await _run_reviewer(input_obj, mock_ctx)

        return mock_dispatch

    async def test_redispatch_fires_on_request_changes(self):
        inp = self._make_input(attempt=0)
        mock_dispatch = await self._run(inp, "REQUEST_CHANGES: fix imports")
        mock_dispatch.assert_called_once()
        call_kwargs = mock_dispatch.call_args[1]
        assert call_kwargs["agent_type"] == "code"
        desc = call_kwargs["task_description"]
        assert "pr: 42" in desc
        assert "attempt: 1" in desc
        assert "branch: praetor-coder/task-1" in desc

    async def test_redispatch_does_not_fire_at_cap(self):
        inp = self._make_input(attempt=1)
        mock_dispatch = await self._run(inp, "REQUEST_CHANGES: still broken")
        mock_dispatch.assert_not_called()

    async def test_redispatch_does_not_fire_on_approve(self):
        inp = self._make_input(attempt=0)
        mock_dispatch = await self._run(inp, "APPROVE: looks good")
        mock_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# 6–7. GitHub webhook — synchronize routing
# ---------------------------------------------------------------------------

class TestWebhookSynchronize:
    def test_synchronize_on_praetor_branch_dispatches(self, web_client):
        c, hatchet = web_client
        payload = _pr_payload(action="synchronize", head_ref="praetor-coder/task-99")
        resp = _post(c, payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["dispatched"] == "github:pr_opened"
        assert data["attempt"] == 1
        hatchet.event.push.assert_called_once()
        pushed = hatchet.event.push.call_args[0][1]
        assert pushed["attempt"] == 1
        assert pushed["head_branch"] == "praetor-coder/task-99"

    def test_synchronize_on_non_praetor_branch_ignored(self, web_client):
        c, hatchet = web_client
        payload = _pr_payload(action="synchronize", head_ref="feature/my-branch")
        resp = _post(c, payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        hatchet.event.push.assert_not_called()
