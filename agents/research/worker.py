"""Hatchet worker: handles agent:research events (Hatchet SDK v1.x)."""
import time
from datetime import timedelta

from hatchet_sdk import Context, Hatchet
from hatchet_sdk.types.concurrency import ConcurrencyExpression, ConcurrencyLimitStrategy
from pydantic import BaseModel
from pydantic_ai.usage import UsageLimits

from .agent import build_agent
from common.langfuse_tools import langfuse_context, observe
from common.metrics import start_metrics_server, task_invocations, task_active, task_duration

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


_AGENT_NAME = "research"


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
        f"Follow your system instructions to search and store findings under agent_id='research'. "
        f"Also write a brief summary to add_memory under agent_id='task-{input.task_id}' (required for status tracking). "
        f"When done, call update_vikunja_task with task_id={input.task_id} and your summary."
    )
    agent = _get_agent()
    task_active.labels(agent=_AGENT_NAME).inc()
    t0 = time.monotonic()
    try:
        result = await agent.run(prompt, usage_limits=UsageLimits(request_limit=30))
        task_invocations.labels(agent=_AGENT_NAME, status="success").inc()
        langfuse_context.update_current_trace(output=result.output)
        return {"summary": result.output, "task_id": input.task_id}
    except Exception:
        task_invocations.labels(agent=_AGENT_NAME, status="error").inc()
        raise
    finally:
        task_active.labels(agent=_AGENT_NAME).dec()
        task_duration.labels(agent=_AGENT_NAME).observe(time.monotonic() - t0)


def main() -> None:
    start_metrics_server(_AGENT_NAME)
    hatchet = Hatchet()

    run_research = hatchet.task(
        name="research",
        on_events=["agent:research"],
        input_validator=ResearchInput,
        execution_timeout=timedelta(minutes=10),
        retries=1,
        concurrency=ConcurrencyExpression(
            expression='"research"',
            max_runs=1,
            limit_strategy=ConcurrencyLimitStrategy.CANCEL_NEWEST,
        ),
    )(_run_research)

    worker = hatchet.worker("research-worker", workflows=[run_research], slots=1)
    worker.start()


if __name__ == "__main__":
    main()
