"""Hatchet worker: handles deploy:staging events."""
import time
from datetime import timedelta

from hatchet_sdk import Context, Hatchet
from pydantic import BaseModel

from .agent import build_agent, SYSTEM_PROMPT
from common.metrics import start_metrics_server, task_invocations, task_active, task_duration
from common.skills import assemble_prompt


class StagingDeployInput(BaseModel):
    repo: str
    pr_number: int | None = None
    branch: str = ""
    deploy_url: str
    commit_sha: str = ""
    author: str = ""


_AGENT_NAME = "qa"


async def _run_qa(input: StagingDeployInput, context: Context) -> dict:
    prompt = (
        f"Run QA for staging deploy of {input.repo}.\n"
        f"Staging URL: {input.deploy_url}\n"
        f"Branch: {input.branch}, Commit: {input.commit_sha}, Author: {input.author}\n\n"
        f"Check the URL is reachable and the page loads without errors. Report results."
    )
    full_prompt = await assemble_prompt(_AGENT_NAME, SYSTEM_PROMPT)
    agent = build_agent(system_prompt=full_prompt)
    task_active.labels(agent=_AGENT_NAME).inc()
    t0 = time.monotonic()
    try:
        result = await agent.run(prompt)
        task_invocations.labels(agent=_AGENT_NAME, status="success").inc()
        return {"result": result.output, "deploy_url": input.deploy_url}
    except Exception:
        task_invocations.labels(agent=_AGENT_NAME, status="error").inc()
        raise
    finally:
        task_active.labels(agent=_AGENT_NAME).dec()
        task_duration.labels(agent=_AGENT_NAME).observe(time.monotonic() - t0)


def main() -> None:
    start_metrics_server(_AGENT_NAME)
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
