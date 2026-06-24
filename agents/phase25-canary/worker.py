"""Hatchet worker: handles agent:phase25-canary events."""
import time
from datetime import timedelta

from hatchet_sdk import Context, Hatchet
from pydantic import BaseModel
from pydantic_ai.usage import UsageLimits

from .agent import build_agent
from common.langfuse_tools import langfuse_context, observe
from common.memory_tools import add_memory, search_memory
from common.metrics import start_metrics_server, task_invocations, task_active, task_duration

_agent = None
_AGENT_NAME = "phase25-canary"


def _get_agent():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


class Phase25CanaryInput(BaseModel):
    task_title: str
    canary: bool = False


@observe(capture_input=False, capture_output=False)
async def _run_phase25_canary(input: Phase25CanaryInput, context: Context) -> dict:
    langfuse_context.update_current_trace(
        name=f"phase25-canary-{input.task_title}",
        input=input.model_dump(),
        tags=["phase25-canary"],
    )
    prior = await search_memory(input.task_title, "phase25-canary")
    prior_context = "\n".join(prior) if prior else "No prior memory found."
    prompt = f"Task: {input.task_title}\n\nPrior context:\n{prior_context}"
    agent = _get_agent()
    task_active.labels(agent=_AGENT_NAME).inc()
    t0 = time.monotonic()
    try:
        result = await agent.run(prompt, usage_limits=UsageLimits(request_limit=50))
        task_invocations.labels(agent=_AGENT_NAME, status="success").inc()
        langfuse_context.update_current_trace(output=result.output)
        await add_memory(f"{input.task_title}: {result.output}", "phase25-canary")
        return {"result": result.output}
    except Exception:
        task_invocations.labels(agent=_AGENT_NAME, status="error").inc()
        raise
    finally:
        task_active.labels(agent=_AGENT_NAME).dec()
        task_duration.labels(agent=_AGENT_NAME).observe(time.monotonic() - t0)


def main() -> None:
    start_metrics_server(_AGENT_NAME)
    hatchet = Hatchet()
    run_phase25_canary = hatchet.task(
        name="phase25-canary",
        on_events=["agent:phase25-canary"],
        execution_timeout=timedelta(minutes=20),
        retries=1,
    )(_run_phase25_canary)
    worker = hatchet.worker("phase25-canary-worker", workflows=[run_phase25_canary], slots=1)
    worker.start()


if __name__ == "__main__":
    main()
