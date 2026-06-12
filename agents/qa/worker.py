"""Hatchet worker: handles deploy:staging events."""
from datetime import timedelta

from hatchet_sdk import Context, Hatchet
from pydantic import BaseModel

from .agent import build_agent

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


class StagingDeployInput(BaseModel):
    repo: str
    pr_number: int | None = None
    branch: str = ""
    deploy_url: str
    commit_sha: str = ""
    author: str = ""


async def _run_qa(input: StagingDeployInput, context: Context) -> dict:
    prompt = (
        f"Run QA for staging deploy of {input.repo}.\n"
        f"Staging URL: {input.deploy_url}\n"
        f"Branch: {input.branch}, Commit: {input.commit_sha}, Author: {input.author}\n\n"
        f"Check the URL is reachable and the page loads without errors. Report results."
    )
    agent = _get_agent()
    result = await agent.run(prompt)
    return {"result": result.data, "deploy_url": input.deploy_url}


def main() -> None:
    hatchet = Hatchet()

    run_qa = hatchet.task(
        name="qa",
        on_events=["deploy:staging"],
        input_validator=StagingDeployInput,
        execution_timeout=timedelta(minutes=10),
        retries=1,
    )(_run_qa)

    worker = hatchet.worker("qa-worker", workflows=[run_qa])
    worker.start()


if __name__ == "__main__":
    main()
