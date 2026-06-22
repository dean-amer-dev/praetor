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

    prompt = f"Task #{task_id}: {task_title}"
    if task_description:
        prompt += f"\n\nDescription: {task_description}"
    prompt += (
        f"\n\nResearch this topic thoroughly. "
        f"Follow your system instructions to search and store findings under agent_id='research'. "
        f"Also write a brief summary to add_memory under agent_id='task-{task_id}' "
        f"(required for status tracking). "
        f"When done, call update_vikunja_task with task_id={task_id} and your summary."
    )

    from agents.research.agent import build_agent

    agent = build_agent()
    result = await agent.run(prompt, usage_limits=UsageLimits(request_limit=30))

    output = {"summary": str(result.output), "task_id": task_id}
    print(json.dumps(output))


if __name__ == "__main__":
    asyncio.run(main())
