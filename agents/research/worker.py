"""Hatchet worker: handles agent:research events (Hatchet SDK v1.x)."""
from datetime import timedelta

from hatchet_sdk import Context, Hatchet
from hatchet_sdk.types.concurrency import ConcurrencyExpression, ConcurrencyLimitStrategy
from pydantic import BaseModel

from .agent import build_agent
from common.langfuse_tools import langfuse_context, observe

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


class ResearchInput(BaseModel):
    task_id: int
    task_title: str
    task_description: str = ""


@observe()
async def _run_research(input: ResearchInput, context: Context) -> dict:
    langfuse_context.update_current_trace(
        name=f"research-task-{input.task_id}",
        input=input.model_dump(),
        tags=["research", f"task-{input.task_id}"],
    )
    prompt = f"Task #{input.task_id}: {input.task_title}"
    if input.task_description:
        prompt += f"\n\nDescription: {input.task_description}"
    prompt += (
        f"\n\nResearch this topic thoroughly. "
        f"Store key findings in memory under agent_id='task-{input.task_id}'. "
        f"When done, call update_vikunja_task with task_id={input.task_id} and your summary."
    )
    agent = _get_agent()
    result = await agent.run(prompt)
    langfuse_context.update_current_trace(output=result.output)
    return {"summary": result.output, "task_id": input.task_id}


def main() -> None:
    hatchet = Hatchet()

    run_research = hatchet.task(
        name="research",
        on_events=["agent:research"],
        input_validator=ResearchInput,
        execution_timeout=timedelta(minutes=10),
        retries=1,
        # One active run per task_id — deduplicates duplicate webhook deliveries
        concurrency=ConcurrencyExpression(
            expression="string(input.task_id)",
            max_runs=1,
            limit_strategy=ConcurrencyLimitStrategy.CANCEL_IN_PROGRESS,
        ),
    )(_run_research)

    worker = hatchet.worker("research-worker", workflows=[run_research], slots=1)
    worker.start()


if __name__ == "__main__":
    main()
