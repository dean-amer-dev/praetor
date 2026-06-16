"""Hatchet worker: handles github:pr_opened events."""
from datetime import timedelta

from hatchet_sdk import Context, Hatchet
from hatchet_sdk.types.concurrency import ConcurrencyExpression, ConcurrencyLimitStrategy
from pydantic import BaseModel

from .agent import build_agent

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


async def _run_reviewer(input: PROpenedInput, context: Context) -> dict:
    prompt = (
        f"Review PR #{input.pr_number} on {input.repo}.\n"
        f"PR URL: {input.pr_url}\n"
        f"Author: {input.author}\n\n"
        f"Fetch the diff, analyze it, and post a structured review comment."
    )
    agent = _get_agent()
    result = await agent.run(prompt)
    return {"result": result.output, "pr_url": input.pr_url}


def main() -> None:
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
            limit_strategy=ConcurrencyLimitStrategy.CANCEL_IN_PROGRESS,
        ),
    )(_run_reviewer)

    worker = hatchet.worker("reviewer-worker", workflows=[run_reviewer])
    worker.start()


if __name__ == "__main__":
    main()
