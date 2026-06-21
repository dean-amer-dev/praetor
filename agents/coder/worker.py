"""Hatchet worker: handles agent:code events (Hatchet SDK v1.x)."""
import os
import re
import shutil
import time
from datetime import timedelta
from pathlib import Path

from hatchet_sdk import Context, Hatchet
from hatchet_sdk.types.concurrency import ConcurrencyExpression, ConcurrencyLimitStrategy
from pydantic import BaseModel
from pydantic_ai.usage import UsageLimits

from .agent import build_agent, update_vikunja_task
from common.langfuse_tools import langfuse_context, observe
from common.metrics import start_metrics_server, task_invocations, task_active, task_duration

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


_AGENT_NAME = "coder"


@observe(capture_input=False, capture_output=False)
async def _run_coder(input: CoderInput, context: Context) -> dict:
    langfuse_context.update_current_trace(
        name=f"coder-task-{input.task_id}",
        input=input.model_dump(),
        tags=["coder", f"task-{input.task_id}"],
    )
    # Clean scratch before each run — emptyDir persists across container restarts within a pod,
    # so a stale partial clone from a previous OOM-killed run would confuse the agent.
    scratch = Path(os.environ.get("SCRATCH_DIR", "/tmp/scratch"))
    scratch.mkdir(parents=True, exist_ok=True)
    for item in scratch.iterdir():
        shutil.rmtree(item) if item.is_dir() else item.unlink()

    repo_match = re.search(r"repo:\s*(\S+)", input.task_description)
    if not repo_match:
        err = "no repo reference found in task description — add 'repo: owner/name' to the description"
        await update_vikunja_task(input.task_id, f"coder error: {err}", done=False)
        task_invocations.labels(agent=_AGENT_NAME, status="error").inc()
        return {"error": err, "task_id": input.task_id}

    prompt = (
        f"Task #{input.task_id}: {input.task_title}\n\n"
        f"Description: {input.task_description}\n\n"
        f"Implement this task on the referenced repo. Create branch praetor-coder/task-{input.task_id}, "
        f"implement, commit, push, open a draft PR. "
        f"When done: "
        f"(1) follow your system instructions to store key decisions under agent_id='coder-{{owner}}/{{repo}}' (replace with the actual repo path), "
        f"(2) also write a brief completion note to add_memory under agent_id='task-{input.task_id}' with the PR URL and what was done (this is required for status tracking), "
        f"(3) post the PR URL as a Vikunja comment on task {input.task_id} and mark it done."
    )
    agent = _get_agent()
    task_active.labels(agent=_AGENT_NAME).inc()
    t0 = time.monotonic()
    try:
        result = await agent.run(
            prompt,
            usage_limits=UsageLimits(request_limit=50),
        )
        task_invocations.labels(agent=_AGENT_NAME, status="success").inc()
        langfuse_context.update_current_trace(output=result.output)
        return {"result": result.output, "task_id": input.task_id}
    except Exception:
        task_invocations.labels(agent=_AGENT_NAME, status="error").inc()
        raise
    finally:
        task_active.labels(agent=_AGENT_NAME).dec()
        task_duration.labels(agent=_AGENT_NAME).observe(time.monotonic() - t0)


def main() -> None:
    start_metrics_server(_AGENT_NAME)
    hatchet = Hatchet()

    run_coder = hatchet.task(
        name="coder",
        on_events=["agent:code"],
        input_validator=CoderInput,
        execution_timeout=timedelta(minutes=20),
        retries=1,
        concurrency=ConcurrencyExpression(
            expression='"coder"',
            max_runs=1,
            limit_strategy=ConcurrencyLimitStrategy.CANCEL_NEWEST,
        ),
    )(_run_coder)

    worker = hatchet.worker("coder-worker", workflows=[run_coder], slots=1)
    worker.start()


if __name__ == "__main__":
    main()
