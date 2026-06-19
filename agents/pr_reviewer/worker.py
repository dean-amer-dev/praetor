"""Hatchet worker: handles github:pr_opened events."""
import time
from datetime import timedelta

from hatchet_sdk import Context, Hatchet
from hatchet_sdk.types.concurrency import ConcurrencyExpression, ConcurrencyLimitStrategy
from pydantic import BaseModel

from .agent import build_agent
from common.metrics import start_metrics_server, task_invocations, task_active, task_duration

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


class PROpenedInput(BaseModel):
    repo: str
    pr_number: str
    pr_url: str = ""
    diff_url: str = ""
    author: str = ""


_AGENT_NAME = "reviewer"


async def _run_reviewer(input: PROpenedInput, context: Context) -> dict:
    prompt = (
        f"Review PR #{input.pr_number} on {input.repo}.\n"
        f"PR URL: {input.pr_url}\n"
        f"Author: {input.author}\n\n"
        f"Fetch the diff, analyze it, and post a structured review comment."
    )
    agent = _get_agent()
    task_active.labels(agent=_AGENT_NAME).inc()
    t0 = time.monotonic()
    try:
        result = await agent.run(prompt)
        task_invocations.labels(agent=_AGENT_NAME, status="success").inc()
        return {"result": result.output, "pr_url": input.pr_url}
    except Exception:
        task_invocations.labels(agent=_AGENT_NAME, status="error").inc()
        raise
    finally:
        task_active.labels(agent=_AGENT_NAME).dec()
        task_duration.labels(agent=_AGENT_NAME).observe(time.monotonic() - t0)


def main() -> None:
    start_metrics_server(_AGENT_NAME)
    hatchet = Hatchet()

    run_reviewer = hatchet.task(
        name="pr-reviewer",
        on_events=["github:pr_opened"],
        input_validator=PROpenedInput,
        execution_timeout=timedelta(minutes=10),
        retries=0,
        concurrency=ConcurrencyExpression(
            expression="input.repo + input.pr_number",
            max_runs=1,
            limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
        ),
    )(_run_reviewer)

    worker = hatchet.worker("reviewer-worker", workflows=[run_reviewer])
    worker.start()


if __name__ == "__main__":
    main()
