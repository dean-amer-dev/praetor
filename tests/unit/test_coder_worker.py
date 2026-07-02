"""Tests for coder worker spec parsing and secondary repo injection."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_description(primary: str, secondary: list[str] | None = None) -> str:
    sec_block = ""
    if secondary:
        items = "\n".join(f'  "{r}",' for r in secondary)
        sec_block = f'\nsecondary = [\n{items}\n]'
    return f"""
```toml
[repos]
primary = "{primary}"{sec_block}

[task]
type = "modify_app"

[app]
changes = ["add feature X"]
```
"""


class TestSecondaryRepoInjection:
    async def _run_with_captured_prompt(self, description: str) -> str:
        from agents.coder.worker import CoderInput, _run_coder
        captured: dict = {}

        async def fake_run(prompt, **kw):
            captured["prompt"] = prompt
            result = MagicMock()
            result.output = "done"
            return result

        with (
            patch("agents.coder.worker.build_agent") as mock_build,
            patch("agents.coder.worker.search_memory", new=AsyncMock(return_value=[])),
            patch("agents.coder.worker.add_memory", new=AsyncMock()),
            patch("agents.coder.worker.get_system_prompt", return_value="sys"),
            patch("agents.coder.worker.assemble_prompt", new=AsyncMock(return_value="sys")),
            patch("agents.coder.worker.update_vikunja_task", new=AsyncMock()),
        ):
            agent = MagicMock()
            agent.run = fake_run
            mock_build.return_value = agent
            ctx = MagicMock()
            await _run_coder(
                CoderInput(task_id=99, task_title="test", task_description=description),
                ctx,
            )
        return captured.get("prompt", "")

    async def test_no_secondary_no_injection(self):
        prompt = await self._run_with_captured_prompt(_make_description("amerenda/ecdysis"))
        assert "secondary-scratch" not in prompt
        assert "Secondary repos" not in prompt

    async def test_secondary_repo_injected(self):
        prompt = await self._run_with_captured_prompt(
            _make_description("amerenda/ecdysis", secondary=["amerenda/k3s-dean-gitops"])
        )
        assert "amerenda/k3s-dean-gitops" in prompt
        assert "secondary-scratch" in prompt
        assert "praetor-coder/task-99" in prompt

    async def test_multiple_secondary_repos_all_injected(self):
        prompt = await self._run_with_captured_prompt(
            _make_description("amerenda/ecdysis",
                              secondary=["amerenda/k3s-dean-gitops", "amerenda/sazed"])
        )
        assert "amerenda/k3s-dean-gitops" in prompt
        assert "amerenda/sazed" in prompt
