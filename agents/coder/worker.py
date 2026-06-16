"""Hatchet worker: handles agent:code events (Hatchet SDK v1.x)."""
import re
from datetime import timedelta

from hatchet_sdk import Context, Hatchet
from hatchet_sdk.types.concurrency import ConcurrencyExpression, ConcurrencyLimitStrategy
from pydantic import BaseModel

from .agent import build_agent, update_vikunja_task
from common.langfuse_tools import langfuse_context, observe

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


class CoderInput(BaseModel):
    task_id: int
    task_title: str
    task_description: str = ""


@observe()
async def _run_coder(input: CoderInput, context: Context) -> dict:
    langfuse_context.update_current_trace(
        name=f"coder-task-{input.task_id}",
        input=input.model_dump(),
        tags=["coder", f"task-{input.task_id}"],
    )
    repo_match = re.search(r"repo:\s*(\S+)", input.task_description)
    if not repo_match:
        err = "no repo reference found in task description — add 'repo: owner/name' to the description"
        await update_vikunja_task(input.task_id, f"coder error: {err}", done=False)
        return {"error": err, "task_id": input.task_id}

    prompt = (
        f"Task #{input.task_id}: {input.task_title}\n\n"
        f"Description: {input.task_description}\n\n"
        f"Implement this task on the referenced repo. Create branch amerenda-coder/task-{input.task_id}, "
        f"implement, commit, push, open a draft PR. Store key decisions in memory under "
        f"agent_id='task-{input.task_id}'. When done, post the PR URL as a Vikunja comment on "
        f"task {input.task_id} and mark it done."
    )
    agent = _get_agent()
    result = await agent.run(prompt)
    langfuse_context.update_current_trace(output=result.output)
    return {"result": result.output, "task_id": input.task_id}


def main() -> None:
    hatchet = Hatchet()

    run_coder = hatchet.task(
        name="coder",
        on_events=["agent:code"],
        input_validator=CoderInput,
        execution_timeout=timedelta(minutes=20),
        retries=1,
        concurrency=ConcurrencyExpression(
            expression="string(input.task_id)",
            max_runs=1,
            limit_strategy=ConcurrencyLimitStrategy.CANCEL_IN_PROGRESS,
        ),
    )(_run_coder)

    worker = hatchet.worker("coder-worker", workflows=[run_coder])
    worker.start()


if __name__ == "__main__":
    main()
