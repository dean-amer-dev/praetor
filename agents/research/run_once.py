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
    if prior_context:
        prompt += f"\n\nPrior memory context (search_memory already called):\n{prior_context}"
        prompt += (
            f"\n\nBuild on these prior findings. Search the web only for gaps or new developments. "
            f"Store new findings under agent_id='research'. "
            f"Call add_memory with agent_id='task-{task_id}' with a brief completion note. "
            f"Call update_vikunja_task when done."
        )
    else:
        prompt += (
            f"\n\nResearch this topic thoroughly. "
            f"Store findings under agent_id='research'. "
            f"Call add_memory with agent_id='task-{task_id}' with a brief completion note. "
            f"Call update_vikunja_task when done."
        )

    from agents.research.agent import build_agent

    agent = build_agent()
    result = await agent.run(prompt, usage_limits=UsageLimits(request_limit=30))

    output = {"summary": str(result.output), "task_id": task_id}
    print(json.dumps(output))


if __name__ == "__main__":
    asyncio.run(main())
