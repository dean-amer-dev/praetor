"""Hatchet worker: handles agent:code events (Hatchet SDK v1.x)."""
import os
import re
import shutil
import time
import tomllib
from datetime import timedelta
from pathlib import Path

from hatchet_sdk import Context, Hatchet
from hatchet_sdk.types.concurrency import ConcurrencyExpression, ConcurrencyLimitStrategy
from pydantic import BaseModel
from pydantic_ai.usage import UsageLimits

from .agent import build_agent, update_vikunja_task, _CODER_SYSTEM_PROMPT_FALLBACK
from common.langfuse_tools import langfuse_context, observe, get_system_prompt
from common.memory_tools import add_memory, search_memory
from common.metrics import start_metrics_server, task_invocations, task_active, task_duration
from common.skills import assemble_prompt


def _parse_spec(description: str) -> dict | None:
    match = re.search(r"```toml\n(.*?)```", description, re.DOTALL)
    if not match:
        return None
    try:
        return tomllib.loads(match.group(1))
    except tomllib.TOMLDecodeError:
        return None


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

    secondary_scratch = scratch / "secondary-scratch"
    secondary_scratch.mkdir(parents=True, exist_ok=True)
    for item in secondary_scratch.iterdir():
        shutil.rmtree(item) if item.is_dir() else item.unlink()

    spec = _parse_spec(input.task_description)

    if spec:
        # Spec-driven path — extract structured fields
        repos = spec.get("repos", {})
        repo = repos.get("primary", "")
        secondary_repos = repos.get("secondary", [])
        if not repo:
            err = "spec missing repos.primary"
            task_invocations.labels(agent=_AGENT_NAME, status="error").inc()
            return {"error": err, "task_id": input.task_id}

        task_section = spec.get("task", {})
        task_type = task_section.get("type", "")
        dispatch_cfg = spec.get("dispatch", {})
        request_limit = dispatch_cfg.get("request_limit", 50)
        redispatch_cap = dispatch_cfg.get("redispatch_cap", 2)

        app_section = spec.get("app", {})
        features = app_section.get("features") or app_section.get("changes") or []
        framework = app_section.get("framework", "")

        pr_section = spec.get("pr", {})
        pr_number = str(pr_section.get("number", "")) if pr_section.get("number") else None
        branch = pr_section.get("branch") or None
        feedback = pr_section.get("feedback", "")
        attempt = pr_section.get("attempt", 0)

        prior_context_lines = spec.get("context", {}).get("prior_memory", [])
        prior_context = "\n".join(prior_context_lines) if prior_context_lines else ""

        # Supplement spec context with live Mem0 search
        memory_agent_id = f"coder-{repo}"
        live_prior = await search_memory(f"{input.task_title} {input.task_description}", memory_agent_id)
        if live_prior:
            prior_context = (prior_context + "\n" if prior_context else "") + "\n".join(live_prior)
        if not prior_context:
            prior_context = "No prior memory found for this repo."

        if task_type == "fix_pr" and pr_number and branch:
            mode_block = (
                f"This is revision attempt {attempt + 1}/{redispatch_cap} for existing PR #{pr_number}.\n"
                f"Branch: {branch}\n"
                f"DO NOT create a new branch. Check out '{branch}' and push your fixes to it.\n"
                f"DO NOT open a new PR. The PR already exists at #{pr_number}.\n"
                f"Review feedback to address:\n{feedback}\n"
            )
        elif task_type == "modify_app":
            mode_block = (
                f"You are modifying an existing app. Create branch praetor-coder/task-{input.task_id}.\n"
                f"Changes required:\n" + "\n".join(f"- {c}" for c in features) + "\n"
            )
        else:
            stack_note = f"Stack: {framework}\n" if framework else ""
            mode_block = (
                f"New app — Create branch praetor-coder/task-{input.task_id}.\n"
                + stack_note
                + "Implement ALL of the following features completely — do not stub:\n"
                + "\n".join(f"- {f}" for f in features) + "\n"
            )
    else:
        # Legacy path — parse repo: and pr: from free-form description
        secondary_repos: list[str] = []
        repo_match = re.search(r"repo:\s*(\S+)", input.task_description)
        if not repo_match:
            err = "no repo reference found in task description — add 'repo: owner/name' to the description"
            await update_vikunja_task(input.task_id, f"coder error: {err}", done=False)
            task_invocations.labels(agent=_AGENT_NAME, status="error").inc()
            return {"error": err, "task_id": input.task_id}

        repo = repo_match.group(1)
        memory_agent_id = f"coder-{repo}"

        pr_match       = re.search(r"pr:\s*(\d+)",       input.task_description)
        branch_match   = re.search(r"branch:\s*(\S+)",   input.task_description)
        attempt_match  = re.search(r"attempt:\s*(\d+)",  input.task_description)
        feedback_match = re.search(r"feedback:\s*(.+)",  input.task_description, re.DOTALL)

        pr_number = pr_match.group(1)            if pr_match      else None
        branch    = branch_match.group(1)        if branch_match  else None
        attempt   = int(attempt_match.group(1))  if attempt_match else 0
        feedback  = feedback_match.group(1).strip() if feedback_match else ""
        request_limit = 50

        prior = await search_memory(f"{input.task_title} {input.task_description}", memory_agent_id)
        prior_context = "\n".join(prior) if prior else "No prior memory found for this repo."

        if pr_number and branch:
            mode_block = (
                f"This is revision attempt {attempt + 1}/2 for existing PR #{pr_number}.\n"
                f"Branch: {branch}\n"
                f"DO NOT create a new branch. Check out '{branch}' and push your fixes to it.\n"
                f"DO NOT open a new PR. The PR already exists at #{pr_number}.\n"
            )
            if feedback:
                mode_block += f"Review feedback to address:\n{feedback}\n"
        else:
            mode_block = (
                f"Create branch praetor-coder/task-{input.task_id}, implement, commit, push, "
                f"then open a draft PR. "
            )

    if secondary_repos:
        sec_lines = ["\nSecondary repos — also make changes here after finishing the primary repo:"]
        for sec in secondary_repos:
            safe = sec.replace("/", "-")
            sec_dir = str(secondary_scratch / safe)
            sec_lines.append(
                f"- {sec}: clone into {sec_dir} using get_github_token(repo='{sec}'), "
                f"create branch praetor-coder/task-{input.task_id}, make the relevant changes, "
                f"commit, push, open a draft PR, include the PR URL in your final summary."
            )
        sec_lines.append("Complete the primary repo first, then secondary repos in order listed.")
        mode_block += "\n".join(sec_lines) + "\n"

    prompt = (
        f"Task #{input.task_id}: {input.task_title}\n\n"
        f"Prior memory context for {repo}:\n{prior_context}\n\n"
        f"Description: {input.task_description}\n\n"
        f"{mode_block}"
        f"After completing, call add_memory(agent_id='{memory_agent_id}') with key decisions, "
        f"and update_vikunja_task(task_id={input.task_id}) with the outcome."
    )
    base_prompt = get_system_prompt("coder-system", fallback=_CODER_SYSTEM_PROMPT_FALLBACK)
    full_prompt = await assemble_prompt(_AGENT_NAME, base_prompt)
    agent = build_agent(system_prompt=full_prompt)
    task_active.labels(agent=_AGENT_NAME).inc()
    t0 = time.monotonic()
    try:
        result = await agent.run(
            prompt,
            usage_limits=UsageLimits(request_limit=request_limit),
        )
        task_invocations.labels(agent=_AGENT_NAME, status="success").inc()
        langfuse_context.update_current_trace(output=result.output)

        await add_memory(
            f"Task #{input.task_id} ({input.task_title}): {result.output}",
            memory_agent_id,
        )
        await add_memory(
            f"coder completed task #{input.task_id}: {input.task_title}. result: {str(result.output)[:500]}",
            f"task-{input.task_id}",
        )
        # planner-global: feed the OWU planning conversation with repo/task outcomes
        if spec:
            infra = spec.get("infra", {})
            hostname = infra.get("hostname", "")
            ns = infra.get("k3s_namespace", "")
            framework = spec.get("app", {}).get("framework", "")
            await add_memory(
                f"{repo}: {input.task_title} completed. "
                + (f"hostname={hostname}. " if hostname else "")
                + (f"namespace={ns}. " if ns else "")
                + (f"stack={framework}. " if framework else "")
                + (f"secondary_repos={secondary_repos}. " if secondary_repos else "")
                + f"task_id={input.task_id}",
                "planner-global",
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
        on_events=["agent:code", "agent:scaffold"],
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
