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
from common.memory_tools import add_memory, search_memory
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

    repo = repo_match.group(1)
    memory_agent_id = f"coder-{repo}"

    pr_match     = re.search(r"pr:\s*(\d+)",       input.task_description)
    branch_match = re.search(r"branch:\s*(\S+)",   input.task_description)
    attempt_match = re.search(r"attempt:\s*(\d+)", input.task_description)

    pr_number = pr_match.group(1)            if pr_match      else None
    branch    = branch_match.group(1)        if branch_match  else None
    attempt   = int(attempt_match.group(1))  if attempt_match else 0

    # Phase 21 condition 6: search_memory is ALWAYS the first operation.
    # Awaited directly (not via run_in_executor) so @observe() creates a child span
    # in the current Langfuse trace, making it visible as the first tool call.
    prior = await search_memory(f"{input.task_title} {input.task_description}", memory_agent_id)
    prior_context = "\n".join(prior) if prior else "No prior memory found for this repo."

    if pr_number and branch:
        # Mode B — edit existing PR
        mode_block = (
            f"This is revision attempt {attempt + 1}/2 for existing PR #{pr_number}.\n"
            f"Branch: {branch}\n"
            f"DO NOT create a new branch. Check out '{branch}' and push your fixes to it.\n"
            f"DO NOT open a new PR. The PR already exists at #{pr_number}.\n"
        )
    else:
        # Mode A — create new PR
        mode_block = (
            f"Create branch praetor-coder/task-{input.task_id}, implement, commit, push, "
            f"then open a draft PR. "
        )

    prompt = (
        f"Task #{input.task_id}: {input.task_title}\n\n"
        f"Prior memory context for {repo}:\n{prior_context}\n\n"
        f"Description: {input.task_description}\n\n"
        f"{mode_block}"
        f"After completing, call add_memory(agent_id='{memory_agent_id}') with key decisions, "
        f"and update_vikunja_task(task_id={input.task_id}) with the outcome."
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

        # Phase 21 condition 6: always store a memory after the task, even if the model skipped it.
        await add_memory(
            f"Task #{input.task_id} ({input.task_title}): {result.output}",
            memory_agent_id,
        )
        await add_memory(
            f"coder completed task #{input.task_id}: {input.task_title}. result: {str(result.output)[:500]}",
            f"task-{input.task_id}",
        )
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
