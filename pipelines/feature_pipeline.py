"""Hatchet worker: pipeline:feature_decompose — sequential per-feature coder runs.

Receives a full Phase-22 TOML spec. Decomposes the features list into sequential
agent.run() calls, each with a clean context window. Uses Mem0 checkpoints for
idempotent crash recovery.
"""
from __future__ import annotations

import time
import tomllib
from datetime import timedelta

from hatchet_sdk import Context, Hatchet
from hatchet_sdk.types.concurrency import ConcurrencyExpression, ConcurrencyLimitStrategy
from pydantic import BaseModel
from pydantic_ai.usage import UsageLimits

from agents.coder.agent import build_agent as build_coder_agent, _CODER_SYSTEM_PROMPT_FALLBACK
from common.langfuse_tools import get_system_prompt
from common.memory_tools import add_memory, search_memory
from common.metrics import start_metrics_server, task_invocations, task_active, task_duration
from common.skills import assemble_prompt as _assemble_coder_prompt

_AGENT_NAME = "feature-pipeline"


class FeaturePipelineInput(BaseModel):
    task_id: int
    task_title: str
    task_description: str = ""  # contains ```toml ... ``` block with full spec


def _parse_features(spec: dict) -> list[str]:
    return spec.get("app", {}).get("features", [])


def _feature_prompt(spec: dict, feature: str, index: int, total: int, task_id: int) -> str:
    repo = spec["repos"]["primary"]
    framework = spec.get("app", {}).get("framework", "")
    branch = f"praetor-coder/task-{task_id}"
    framework_line = f"Framework: {framework}\n" if framework else ""
    return (
        f"Task #{task_id} — feature {index + 1}/{total}\n"
        f"Repo: {repo}\n"
        f"Branch: {branch} (already exists — clone it, do not create a new branch)\n"
        f"{framework_line}"
        f"\nImplement this ONE feature completely. Do not stub it.\n"
        f"Feature: {feature}\n\n"
        f"When done: commit all changes with message 'feat: {feature}' and push to the branch.\n"
        f"Do NOT open a PR. The pipeline opens the PR after all features are done.\n"
        f"Call save_progress with task_id={task_id}, done=['{feature}'], remaining=<remaining list>."
    )


def _setup_branch_prompt(spec: dict, task_id: int) -> str:
    repo = spec["repos"]["primary"]
    branch = f"praetor-coder/task-{task_id}"
    return (
        f"Task #{task_id} — branch setup\n"
        f"Repo: {repo}\n\n"
        f"This is a freshly created repo. Clone it, create branch '{branch}', "
        f"and push the empty branch. Do not implement anything yet.\n"
        f"Call save_progress with task_id={task_id}, done=['branch-setup'], remaining=[]."
    )


def _finalize_prompt(spec: dict, features: list[str], task_id: int) -> str:
    repo = spec["repos"]["primary"]
    branch = f"praetor-coder/task-{task_id}"
    title = spec["task"]["title"]
    feat_list = "\n".join(f"- {f}" for f in features)
    return (
        f"Task #{task_id} — open draft PR\n"
        f"Repo: {repo}, branch: {branch}\n\n"
        f"All features are implemented and committed on the branch. Open a draft PR titled:\n"
        f"'feat: {title}'\n\n"
        f"PR description should list all implemented features:\n{feat_list}\n\n"
        f"Call update_vikunja_task with task_id={task_id} and the PR URL when done."
    )


def _extract_spec(description: str) -> dict | None:
    import re
    match = re.search(r"```toml\n(.*?)```", description, re.DOTALL)
    if not match:
        return None
    try:
        return tomllib.loads(match.group(1))
    except tomllib.TOMLDecodeError:
        return None


async def _run_feature_pipeline(input: FeaturePipelineInput, context: Context) -> dict:
    spec = _extract_spec(input.task_description)
    if not spec:
        raise ValueError(f"task_description for task {input.task_id} contains no TOML spec block")

    features = _parse_features(spec)
    if not features:
        raise ValueError(f"spec for task {input.task_id} has no app.features list")

    task_agent_id = f"task-{input.task_id}"
    request_limit = spec.get("dispatch", {}).get("request_limit", 50)
    task_type = spec.get("task", {}).get("type", "modify_app")

    task_active.labels(agent=_AGENT_NAME).inc()
    t0 = time.monotonic()
    coder_base = get_system_prompt("coder-system", fallback=_CODER_SYSTEM_PROMPT_FALLBACK)
    coder_prompt = await _assemble_coder_prompt("coder", coder_base)
    try:
        # For new_app specs: set up the initial branch before the feature loop
        if task_type == "new_app":
            hits = await search_memory("branch-setup", task_agent_id)
            if not any("branch-setup" in h.lower() for h in hits):
                agent = build_coder_agent(system_prompt=coder_prompt)
                await agent.run(
                    _setup_branch_prompt(spec, input.task_id),
                    usage_limits=UsageLimits(request_limit=10),
                )
                await add_memory("branch-setup done", task_agent_id)

        for i, feature in enumerate(features):
            # Crash recovery: skip features already committed to Mem0
            hits = await search_memory(f"feature done: {feature}", task_agent_id)
            if any("done" in h.lower() and feature.lower()[:20] in h.lower() for h in hits):
                continue

            prompt = _feature_prompt(spec, feature, i, len(features), input.task_id)
            agent = build_coder_agent(system_prompt=coder_prompt)
            await agent.run(prompt, usage_limits=UsageLimits(request_limit=request_limit))
            await add_memory(f"feature done: {feature}", task_agent_id)

        # Open the draft PR
        agent = build_coder_agent(system_prompt=coder_prompt)
        result = await agent.run(
            _finalize_prompt(spec, features, input.task_id),
            usage_limits=UsageLimits(request_limit=20),
        )

        await add_memory(
            f"task-{input.task_id} ({input.task_title}) complete via feature pipeline: "
            f"{str(result.output)[:300]}",
            "planner-global",
        )

        task_invocations.labels(agent=_AGENT_NAME, status="success").inc()
        return {"result": str(result.output), "task_id": input.task_id}
    except Exception:
        task_invocations.labels(agent=_AGENT_NAME, status="error").inc()
        raise
    finally:
        task_active.labels(agent=_AGENT_NAME).dec()
        task_duration.labels(agent=_AGENT_NAME).observe(time.monotonic() - t0)


def main() -> None:
    start_metrics_server(_AGENT_NAME)
    hatchet = Hatchet()

    run_pipeline = hatchet.task(
        name="feature-pipeline",
        on_events=["pipeline:feature_decompose"],
        input_validator=FeaturePipelineInput,
        execution_timeout=timedelta(minutes=120),  # covers N features at ~15 min each + finalize
        retries=1,
        concurrency=ConcurrencyExpression(
            expression='"feature_pipeline"',
            max_runs=1,
            limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
        ),
    )(_run_feature_pipeline)

    worker = hatchet.worker("feature-pipeline-worker", workflows=[run_pipeline], slots=1)
    worker.start()


if __name__ == "__main__":
    main()
