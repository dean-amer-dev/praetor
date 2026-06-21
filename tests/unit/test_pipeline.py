"""Unit tests for pipelines/research_then_code.py — node logic."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipelines.research_then_code import PipelineState, ResearchNode, CoderNode


@pytest.fixture
def state():
    return PipelineState(task_id=42, task_title="Build feature X", task_description="repo: amerenda/ecdysis")


class TestPipelineState:
    def test_defaults(self):
        s = PipelineState(task_id=1, task_title="title")
        assert s.task_description == ""
        assert s.task_id == 1
        assert s.task_title == "title"

    def test_with_description(self):
        s = PipelineState(1, "t", "desc")
        assert s.task_description == "desc"


class TestResearchNode:
    async def test_skips_when_memories_exist(self, state):
        with patch("pipelines.research_then_code.get_all_memories", return_value=["fact1", "fact2"]):
            with patch("pipelines.research_then_code.build_research_agent") as mock_build:
                node = ResearchNode()
                result = await node.run(state)
        assert result["skipped"] is True
        mock_build.assert_not_called()

    async def test_runs_agent_when_no_memories(self, state):
        mock_agent = AsyncMock()
        mock_agent.run.return_value = MagicMock(output="research summary")
        with patch("pipelines.research_then_code.get_all_memories", return_value=[]):
            with patch("pipelines.research_then_code.build_research_agent", return_value=mock_agent):
                node = ResearchNode()
                result = await node.run(state)
        assert result["skipped"] is False
        mock_agent.run.assert_called_once()

    async def test_returns_summary(self, state):
        mock_agent = AsyncMock()
        mock_agent.run.return_value = MagicMock(output="my summary text")
        with patch("pipelines.research_then_code.get_all_memories", return_value=[]):
            with patch("pipelines.research_then_code.build_research_agent", return_value=mock_agent):
                node = ResearchNode()
                result = await node.run(state)
        assert result["summary"] == "my summary text"

    async def test_cached_memories_as_summary(self, state):
        memories = [f"fact{i}" for i in range(15)]
        with patch("pipelines.research_then_code.get_all_memories", return_value=memories):
            node = ResearchNode()
            result = await node.run(state)
        # Only first 10 used
        assert result["summary"].count("fact") == 10

    async def test_task_id_in_result(self, state):
        with patch("pipelines.research_then_code.get_all_memories", return_value=["x"]):
            node = ResearchNode()
            result = await node.run(state)
        assert result["task_id"] == 42


class TestCoderNode:
    async def test_fails_without_repo_in_description(self):
        state = PipelineState(task_id=1, task_title="title", task_description="no repo here")
        with patch("pipelines.research_then_code.coder_vikunja", new=AsyncMock()) as mock_vikunja:
            node = CoderNode()
            result = await node.run(state, {"summary": "research"})
        assert "error" in result
        assert "repo" in result["error"]
        mock_vikunja.assert_called_once()
        # Confirm done=False (not marking task done on failure)
        call_kwargs = mock_vikunja.call_args[1]
        assert call_kwargs.get("done") is False

    async def test_proceeds_with_repo_in_description(self):
        state = PipelineState(
            task_id=5,
            task_title="Add healthz",
            task_description="repo: amerenda/ecdysis — add a /healthz endpoint",
        )
        mock_agent = AsyncMock()
        mock_agent.run.return_value = MagicMock(output="done")
        with patch("pipelines.research_then_code.build_coder_agent", return_value=mock_agent):
            with patch("pipelines.research_then_code.coder_vikunja", new=AsyncMock()):
                node = CoderNode()
                result = await node.run(state, {"summary": "research findings"})
        mock_agent.run.assert_called_once()
        assert result["task_id"] == 5

    async def test_research_context_in_prompt(self):
        state = PipelineState(1, "t", "repo: amerenda/x")
        mock_agent = AsyncMock()
        mock_agent.run.return_value = MagicMock(output="done")
        with patch("pipelines.research_then_code.build_coder_agent", return_value=mock_agent):
            with patch("pipelines.research_then_code.coder_vikunja", new=AsyncMock()):
                node = CoderNode()
                await node.run(state, {"summary": "unique-research-text-xyz"})
        prompt = mock_agent.run.call_args[0][0]
        assert "unique-research-text-xyz" in prompt

    async def test_branch_name_in_prompt(self):
        state = PipelineState(77, "title", "repo: amerenda/foo")
        mock_agent = AsyncMock()
        mock_agent.run.return_value = MagicMock(output="done")
        with patch("pipelines.research_then_code.build_coder_agent", return_value=mock_agent):
            with patch("pipelines.research_then_code.coder_vikunja", new=AsyncMock()):
                node = CoderNode()
                await node.run(state, {"summary": ""})
        prompt = mock_agent.run.call_args[0][0]
        assert "praetor-coder/task-77" in prompt
