"""Pipeline nodes: ResearchNode → CoderNode, using Mem0 as the research handoff.

ResearchNode stores findings in the task-scoped Mem0 namespace; CoderNode reads
them back to seed the implementation prompt.  Both nodes are idempotent — if
Mem0 already contains entries for the task, the research step is skipped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from agents.coder.agent import build_agent as build_coder_agent
from agents.coder.agent import update_vikunja_task as coder_vikunja
from agents.research.agent import build_agent as build_research_agent
from common.memory_tools import get_all_memories


@dataclass
class PipelineState:
    task_id: int
    task_title: str
    task_description: str = ""


@dataclass
class ResearchNode:
    """Run research agent; idempotent — returns cached memories if already stored."""

    async def run(self, state: PipelineState) -> dict:
        agent_id = f"task-{state.task_id}"

        existing = get_all_memories(agent_id)
        if existing:
            return {
                "task_id": state.task_id,
                "summary": "\n".join(existing[:10]),
                "skipped": True,
            }

        prompt = f"Task #{state.task_id}: {state.task_title}"
        if state.task_description:
            prompt += f"\n\nDescription: {state.task_description}"
        prompt += (
            f"\n\nResearch this topic thoroughly. "
            f"Store key findings in memory under agent_id='{agent_id}'. "
            f"When done, call update_vikunja_task with task_id={state.task_id} and your summary."
        )

        agent = build_research_agent()
        result = await agent.run(prompt)
        return {
            "task_id": state.task_id,
            "summary": str(result.output),
            "skipped": False,
        }


@dataclass
class CoderNode:
    """Run coder agent seeded with research findings from Mem0."""

    async def run(self, state: PipelineState, research: dict) -> dict:
        repo_match = re.search(r"repo:\s*(\S+)", state.task_description)
        if not repo_match:
            err = "no 'repo: owner/name' found in task description"
            await coder_vikunja(state.task_id, f"pipeline error: {err}", done=False)
            return {"error": err, "task_id": state.task_id}

        research_context = research.get("summary", "")
        prompt = (
            f"Task #{state.task_id}: {state.task_title}\n\n"
            f"Description: {state.task_description}\n\n"
            f"Research context from the research phase:\n{research_context}\n\n"
            f"Implement this task on the referenced repo. "
            f"Create branch amerenda-coder/task-{state.task_id}, implement, commit, push, open a draft PR. "
            f"Incorporate the research findings into your implementation. "
            f"Store key decisions in memory under agent_id='task-{state.task_id}'. "
            f"When done, post the PR URL as a Vikunja comment on task {state.task_id} and mark it done."
        )

        agent = build_coder_agent()
        result = await agent.run(prompt)
        return {"result": str(result.output), "task_id": state.task_id}
