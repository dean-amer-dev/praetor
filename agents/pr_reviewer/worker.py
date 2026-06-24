"""Hatchet worker: handles github:pr_opened events."""
import time
from datetime import timedelta

import httpx
import os

from hatchet_sdk import Context, Hatchet
from hatchet_sdk.types.concurrency import ConcurrencyExpression, ConcurrencyLimitStrategy
from pydantic import BaseModel

from .agent import build_agent, SYSTEM_PROMPT
from common.dispatch import dispatch_agent
from common.memory_tools import add_memory
from common.metrics import start_metrics_server, task_invocations, task_active, task_duration
from common.skills import assemble_prompt


class PROpenedInput(BaseModel):
    repo: str
    pr_number: str
    pr_url: str = ""
    diff_url: str = ""
    author: str = ""
    head_branch: str = ""   # populated by webhook handler
    attempt: int = 0        # 0 = first review, 1 = review after first re-dispatch


_AGENT_NAME = "reviewer"


def _get_pr_branch(repo: str, pr_number: str) -> str:
    from common.github_app import get_reviewer_installation_token
    token = get_reviewer_installation_token()
    resp = httpx.get(
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("head", {}).get("ref", "")


async def _run_reviewer(input: PROpenedInput, context: Context) -> dict:
    prompt = (
        f"Review PR #{input.pr_number} on {input.repo}.\n"
        f"PR URL: {input.pr_url}\n"
        f"Author: {input.author}\n\n"
        f"Fetch the diff, analyze it, and post a structured review comment."
    )
    full_prompt = await assemble_prompt(_AGENT_NAME, SYSTEM_PROMPT)
    agent = build_agent(system_prompt=full_prompt)
    task_active.labels(agent=_AGENT_NAME).inc()
    t0 = time.monotonic()
    try:
        result = await agent.run(prompt)
        task_invocations.labels(agent=_AGENT_NAME, status="success").inc()

        # Re-dispatch coder if reviewer requested changes and we haven't hit the cap
        if "REQUEST_CHANGES" in str(result.output) and input.attempt < 1:
            head_branch = input.head_branch or _get_pr_branch(input.repo, input.pr_number)
            if head_branch:
                redispatch_description = (
                    f"Fix review feedback on existing PR.\n"
                    f"repo: {input.repo}\n"
                    f"pr: {input.pr_number}\n"
                    f"branch: {head_branch}\n"
                    f"attempt: {input.attempt + 1}\n\n"
                    f"Review feedback:\n{str(result.output)[:2000]}"
                )
                dispatch_agent(
                    task_id=0,
                    task_title=f"Fix review feedback: PR #{input.pr_number} on {input.repo}",
                    task_description=redispatch_description,
                    agent_type="code",
                )

        # planner-global: record review outcome so planning context tracks PR state
        await add_memory(
            f"{input.repo} PR #{input.pr_number}: review completed. "
            f"outcome={'REQUEST_CHANGES' if 'REQUEST_CHANGES' in str(result.output) else 'APPROVED'}. "
            f"attempt={input.attempt}",
            "planner-global",
        )
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
