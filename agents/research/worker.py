"""Hatchet worker: handles agent:research events (Hatchet SDK v1.x)."""
import asyncio
import json
import os
import sys
import time
from datetime import timedelta

from hatchet_sdk import Context, Hatchet
from hatchet_sdk.types.concurrency import ConcurrencyExpression, ConcurrencyLimitStrategy
from pydantic import BaseModel

from common.langfuse_tools import langfuse_context, observe
from common.metrics import start_metrics_server, task_invocations, task_active, task_duration


class ResearchInput(BaseModel):
    task_id: int
    task_title: str
    task_description: str = ""


_AGENT_NAME = "research"
_SUBPROCESS_TIMEOUT = 600  # 10 minutes


@observe()
async def _run_research(input: ResearchInput, context: Context) -> dict:
    langfuse_context.update_current_trace(
        name=f"research-task-{input.task_id}",
        input=input.model_dump(),
        tags=["research", f"task-{input.task_id}"],
    )

    task_active.labels(agent=_AGENT_NAME).inc()
    t0 = time.monotonic()
    try:
        # Run the agent in an isolated subprocess to prevent Hatchet SDK memory accumulation
        # from leaking into the worker process. Each task gets a clean Python heap.
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "agents.research.run_once",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        stdin_data = json.dumps(input.model_dump()).encode()

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_data),
                timeout=_SUBPROCESS_TIMEOUT,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"research subprocess timed out after {_SUBPROCESS_TIMEOUT}s")

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[-2000:]
            raise RuntimeError(f"research subprocess failed (exit {proc.returncode}): {err}")

        result = json.loads(stdout.decode())
        task_invocations.labels(agent=_AGENT_NAME, status="success").inc()
        langfuse_context.update_current_trace(output=result.get("summary", ""))
        return result
    except Exception:
        task_invocations.labels(agent=_AGENT_NAME, status="error").inc()
        raise
    finally:
        task_active.labels(agent=_AGENT_NAME).dec()
        task_duration.labels(agent=_AGENT_NAME).observe(time.monotonic() - t0)


def main() -> None:
    start_metrics_server(_AGENT_NAME)
    hatchet = Hatchet()

    run_research = hatchet.task(
        name="research",
        on_events=["agent:research"],
        input_validator=ResearchInput,
        execution_timeout=timedelta(minutes=10),
        retries=1,
        concurrency=ConcurrencyExpression(
            expression='"research"',
            max_runs=1,
            limit_strategy=ConcurrencyLimitStrategy.CANCEL_NEWEST,
        ),
    )(_run_research)

    worker = hatchet.worker("research-worker", workflows=[run_research], slots=1)
    worker.start()


if __name__ == "__main__":
    main()
