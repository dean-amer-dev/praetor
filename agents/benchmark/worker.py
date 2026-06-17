"""Hatchet worker: handles agent:benchmark events."""
from __future__ import annotations

from datetime import timedelta

from hatchet_sdk import Context, Hatchet
from pydantic import BaseModel

from .agent import _langfuse, run_research_benchmark, run_reviewer_benchmark, score_output
from common.langfuse_tools import langfuse_context, observe


class BenchmarkInput(BaseModel):
    dataset_name: str
    dataset_item_id: str
    agent_type: str        # "research" | "review"
    model: str = "qwen3-35b"
    prompt_version: str = ""
    run_name: str = ""     # Langfuse experiment name; auto-generated if empty


@observe()
async def _run_benchmark(input: BenchmarkInput, context: Context) -> dict:
    import time

    lf = _langfuse()
    run_name = input.run_name or f"{input.dataset_name}-{int(time.time())}"

    langfuse_context.update_current_trace(
        name=f"benchmark-{input.dataset_name}-{input.dataset_item_id}",
        input=input.model_dump(),
        tags=["benchmark", input.dataset_name, input.agent_type],
    )

    # Fetch dataset item
    dataset = lf.get_dataset(input.dataset_name)
    item = next((i for i in dataset.items if i.id == input.dataset_item_id), None)
    if item is None:
        raise ValueError(f"dataset item {input.dataset_item_id} not found in {input.dataset_name}")

    item_input: dict = item.input if isinstance(item.input, dict) else {"input": str(item.input)}
    expected: dict = item.expected_output if isinstance(item.expected_output, dict) else {}

    # Run the target agent
    agent_id = f"benchmark-{input.dataset_name}-{input.dataset_item_id}"
    if input.agent_type == "research":
        agent_output = await run_research_benchmark(item_input, agent_id)
    elif input.agent_type == "review":
        agent_output = await run_reviewer_benchmark(item_input)
    else:
        raise ValueError(f"unknown agent_type: {input.agent_type!r}")

    # Score via LLM judge
    score_value, reason = score_output(agent_output, expected)

    # Write score to Langfuse and link to dataset item
    trace_id = langfuse_context.get_current_trace_id()
    lf.score(
        trace_id=trace_id,
        name="benchmark-score",
        value=score_value,
        comment=reason,
    )
    item.link(trace_id=trace_id, run_name=run_name)

    langfuse_context.update_current_trace(output={"score": score_value, "reason": reason})
    return {
        "dataset_name": input.dataset_name,
        "item_id": input.dataset_item_id,
        "score": score_value,
        "reason": reason,
        "run_name": run_name,
    }


def main() -> None:
    hatchet = Hatchet()

    run_benchmark = hatchet.task(
        name="benchmark",
        on_events=["agent:benchmark"],
        input_validator=BenchmarkInput,
        execution_timeout=timedelta(minutes=15),
        retries=0,
    )(_run_benchmark)

    worker = hatchet.worker("benchmark-worker", workflows=[run_benchmark], slots=1)
    worker.start()


if __name__ == "__main__":
    main()
