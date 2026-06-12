"""Hatchet DAG worker: research → code pipeline for tasks labeled ai-research + ai-go."""
from datetime import timedelta

from hatchet_sdk import Context, Hatchet
from hatchet_sdk.types.concurrency import ConcurrencyExpression, ConcurrencyLimitStrategy
from pydantic import BaseModel

from .research_then_code import CoderNode, PipelineState, ResearchNode


class PipelineInput(BaseModel):
    task_id: int
    task_title: str
    task_description: str = ""


hatchet = Hatchet()

pipeline = hatchet.workflow(
    name="research-then-code",
    on_events=["pipeline:research_code"],
    input_validator=PipelineInput,
    concurrency=ConcurrencyExpression(
        expression="input.task_id",
        max_runs=1,
        limit_strategy=ConcurrencyLimitStrategy.CANCEL_IN_PROGRESS,
    ),
)


@pipeline.task(
    name="research",
    execution_timeout=timedelta(minutes=10),
    retries=1,
)
async def research_step(input: PipelineInput, ctx: Context) -> dict:
    state = PipelineState(
        task_id=input.task_id,
        task_title=input.task_title,
        task_description=input.task_description,
    )
    node = ResearchNode()
    try:
        return await node.run(state)
    except Exception as exc:
        try:
            from agents.research.agent import update_vikunja_task
            await update_vikunja_task(input.task_id, f"research phase failed: {exc}", done=False)
        except Exception:
            pass
        raise


@pipeline.task(
    name="code",
    parents=[research_step],
    execution_timeout=timedelta(minutes=20),
    retries=1,
)
async def code_step(input: PipelineInput, ctx: Context) -> dict:
    research_output = ctx.task_output(research_step)
    state = PipelineState(
        task_id=input.task_id,
        task_title=input.task_title,
        task_description=input.task_description,
    )
    node = CoderNode()
    return await node.run(state, research_output)


def main() -> None:
    worker = hatchet.worker("pipeline-worker", workflows=[pipeline])
    worker.start()


if __name__ == "__main__":
    main()
