"""Hatchet worker: handles agent:phase25-smoke events."""
import logging
import time
from datetime import timedelta

from hatchet_sdk import Context, Hatchet
from pydantic import BaseModel
from pydantic_ai.usage import UsageLimits

from .agent import build_agent
from common.langfuse_tools import langfuse_context, observe
from common.memory_tools import add_memory, search_memory
from common.metrics import start_metrics_server, task_invocations, task_active, task_duration

logger = logging.getLogger(__name__)

_agent = None
_AGENT_NAME = "phase25-smoke"


def _get_agent():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


class Phase25SmokeInput(BaseModel):
    task_title: str
    canary: bool = False


@observe(capture_input=False, capture_output=False)
async def _run_phase25_smoke(input: Phase25SmokeInput, context: Context) -> dict:
    langfuse_context.update_current_trace(
        name=f"phase25-smoke-{input.task_title}",
        input=input.model_dump(),
        tags=["phase25-smoke"],
    )
    try:
        prior = await search_memory(input.task_title, "phase25-smoke")
    except Exception:
        logger.exception("Memory search failed for task %s", input.task_title)
        prior_context = "No prior memory found (search error)."
    else:
        prior_context = "\n".join(prior) if prior else "No prior memory found."

    prompt = f"Task: {input.task_title}\n\nPrior context:\n{prior_context}"
    agent = _get_agent()
    task_active.labels(agent=_AGENT_NAME).inc()
    t0 = time.monotonic()
    try:
        result = await agent.run(prompt, usage_limits=UsageLimits(request_limit=50))
        task_invocations.labels(agent=_AGENT_NAME, status="success").inc()
        langfuse_context.update_current_trace(output=result.output)
        try:
            await add_memory(f"{input.task_title}: {result.output}", "phase25-smoke")
        except Exception:
            logger.exception("Memory add failed for task %s", input.task_title)
        return {"result": result.output}
    except Exception as exc:
        task_invocations.labels(agent=_AGENT_NAME, status="error").inc()
        langfuse_context.update_current_trace(error=str(exc))
        raise
    finally:
        task_active.labels(agent=_AGENT_NAME).dec()
        task_duration.labels(agent=_AGENT_NAME).observe(time.monotonic() - t0)


def main() -> None:
    start_metrics_server(_AGENT_NAME)
    hatchet = Hatchet()
    run_phase25_smoke = hatchet.task(
        name="phase25-smoke",
        on_events=["agent:phase25-smoke"],
        execution_timeout=timedelta(minutes=20),
        retries=1,
    )(_run_phase25_smoke)
    worker = hatchet.worker("phase25-smoke-worker", workflows=[run_phase25_smoke], slots=1)
    worker.start()


if __name__ == "__main__":
    main()
