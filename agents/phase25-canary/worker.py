"""Hatchet worker: handles agent:phase25-canary events."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
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
_AGENT_NAME = "phase25-canary"


def _get_agent():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


class Phase25CanaryInput(BaseModel):
    task_title: str
    canary: bool = False


_executor = ThreadPoolExecutor(max_workers=4)


def _sync_search(title, agent_name):
    """Run sync search_memory in a thread to avoid blocking the event loop."""
    return search_memory(title, agent_name)


def _sync_add(text, agent_name):
    """Run sync add_memory in a thread to avoid blocking the event loop."""
    return add_memory(text, agent_name)


@observe(capture_input=False, capture_output=False)
async def _run_phase25_canary(input: Phase25CanaryInput, context: Context) -> dict:
    langfuse_context.update_current_trace(
        name=f"phase25-canary-{input.task_title}",
        input=input.model_dump(),
        tags=["phase25-canary"],
    )

    # Search prior memory with graceful degradation
    try:
        loop = context.loop if hasattr(context, "loop") else None
        if loop and not getattr(loop, "_running", True):
            prior = await context.run_in_executor(
                _executor, lambda: search_memory(input.task_title, "phase25-canary")
            )
        else:
            prior = await context.run_in_executor(
                _executor, lambda: search_memory(input.task_title, "phase25-canary")
            )
    except Exception as exc:
        logger.warning("Failed to search memory for '%s': %s", input.task_title, exc)
        prior = []

    prior_context = "\n".join(prior) if prior else "No prior memory found."
    prompt = f"Task: {input.task_title}\n\nPrior context:\n{prior_context}"
    agent = _get_agent()
    task_active.labels(agent=_AGENT_NAME).inc()
    t0 = time.monotonic()
    try:
        result = await agent.run(prompt, usage_limits=UsageLimits(request_limit=50))
        task_invocations.labels(agent=_AGENT_NAME, status="success").inc()
        langfuse_context.update_current_trace(output=result.output)

        # Add memory with graceful degradation
        try:
            await context.run_in_executor(
                _executor, lambda: add_memory(f"{input.task_title}: {result.output}", "phase25-canary")
            )
        except Exception as exc:
            logger.warning("Failed to add memory for '%s': %s", input.task_title, exc)

        return {"result": result.output}
    except Exception as exc:
        task_invocations.labels(agent=_AGENT_NAME, status="error").inc()
        langfuse_context.update_current_trace(error=exc)
        logger.error("Agent run failed for '%s': %s", input.task_title, exc, exc_info=True)
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
