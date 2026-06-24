"""Subprocess entry point: run one research task, print JSON result to stdout.

Invoked by worker.py via asyncio.create_subprocess_exec to isolate memory per task.
Receives task JSON via stdin, writes result JSON to stdout.
"""
import asyncio
import json
import sys

from pydantic_ai.usage import UsageLimits


async def main() -> None:
    raw = sys.stdin.read()
    data = json.loads(raw)

    task_id: int = data["task_id"]
    task_title: str = data["task_title"]
    task_description: str = data.get("task_description", "")
    prior_context: str = data.get("prior_context", "")

    prompt = f"Task #{task_id}: {task_title}"
    if task_description:
        prompt += f"\n\nDescription: {task_description}"
    # Always start with search_memory per Phase 21 condition 7.
    # Prior context is included as a hint, but the agent must still call search_memory explicitly.
    if prior_context:
        prompt += (
            f"\n\nNote: preliminary search_memory found these prior findings:\n{prior_context}"
            f"\n\nCall search_memory(query='{task_title}', agent_id='research') to confirm, "
            f"then search the web for gaps or new developments. "
            f"Store new findings under agent_id='research'. "
            f"Call add_memory with agent_id='task-{task_id}' with a brief completion note. "
            f"Call update_vikunja_task when done."
        )
    else:
        prompt += (
            f"\n\nCall search_memory(query='{task_title}', agent_id='research') first. "
            f"Research this topic thoroughly. "
            f"Store findings under agent_id='research'. "
            f"Call add_memory with agent_id='task-{task_id}' with a brief completion note. "
            f"Call update_vikunja_task when done."
        )

    from agents.research.agent import build_agent, _RESEARCH_SYSTEM_PROMPT_FALLBACK
    from common.langfuse_tools import get_system_prompt
    from common.skills import assemble_prompt

    base = get_system_prompt("research-system", fallback=_RESEARCH_SYSTEM_PROMPT_FALLBACK)
    full_prompt = await assemble_prompt("research", base)
    agent = build_agent(system_prompt=full_prompt)
    result = await agent.run(prompt, usage_limits=UsageLimits(request_limit=30))

    output = {"summary": str(result.output), "task_id": task_id}
    print(json.dumps(output))


if __name__ == "__main__":
    asyncio.run(main())
