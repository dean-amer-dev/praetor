# Phase 28 — Coder Context Management + Feature Decomposition

**Goal:** Fix the coder agent dying mid-task on broad specs. Three layered solutions in
order of effort: truncate noisy tool outputs (immediate), add Mem0 progress checkpoints
(resume after crash), decompose multi-feature specs into sequential per-feature coder
runs (eliminate context exhaustion structurally).

**Prerequisite phases:** 21 (Mem0 active), 27 (spec layer — provides the `features` list)
**Informs phases:** 24 (arbitration loop assumes coder can complete; decomposition helps)

---

## The Problem

The coder runs as a single `agent.run()` call with `request_limit=50`. A broad task
("implement a blog with 4 features") accumulates tool outputs in the pydantic-ai message
history: git clone output, file reads, shell output from npm/pip, write confirmations,
error messages. By tool call 35-40 the context is full of stale noise and the agent
either hits the request limit mid-implementation or halts with degraded output.

**What the research says (JetBrains, Anthropic, OpenHands — all 2025):**
- Simply truncating old tool outputs cuts cost 50%+ and matches or beats LLM summarization
  (JetBrains SWE-bench study with Qwen3-Coder 480B — same model family as ours)
- Structured note-taking to external memory (Mem0) enables crash recovery without
  re-doing completed work
- Task decomposition into sub-tasks with clean context per sub-task is the structural fix
  for tasks too large for any context window

---

## 28a — Tool Result Truncation

**Effort:** ~30 minutes. **Impact:** Recovers 30-40% of context budget on a typical run.

The noisiest tool outputs are shell commands (git clone, npm install, failed builds) and
file reads. Both tools already exist in `agents/coder/agent.py`. Cap their return values
before they enter the message history.

### `run_shell` — keep the tail, not the head

Errors appear at the end of output. A successful `git clone` produces a multi-KB progress
bar that is never useful again after the first read.

```python
async def run_shell(cmd: str) -> str:
    # ... existing logic ...
    output = (result.stdout + result.stderr).strip()
    limit = 3000
    if len(output) > limit:
        # Keep the tail — errors are at the end
        return f"[...{len(output) - limit} chars truncated...]\n" + output[-limit:]
    return output or f"(exit {result.returncode})"
```

### `read_file` — reduce cap from 16KB to 8KB

The agent rarely needs a full file in one read; it needs the relevant section. 8KB covers
~250 lines, which is enough for the agent to understand structure and find the right place
to edit.

```python
return resp.text[:8000]   # was 16000
```

### Why not LLM summarization of tool results?

JetBrains research tested observation masking (truncation) vs LLM summarization on
SWE-bench. Truncation won in 4 out of 5 settings — it is faster, cheaper, and avoids
introducing hallucinations via the summarizer. The summarizer is also another LLM call,
which burns the GPU we're trying to save.

---

## 28b — Mem0 Progress Checkpointing

**Effort:** ~2 hours. **Impact:** Enables resume after crash; eliminates full-restart retries.

### New tool: `save_progress`

Add to `agents/coder/agent.py`:

```python
async def save_progress(done: list[str], remaining: list[str], notes: str = "") -> str:
    """
    Checkpoint task progress to Mem0. Call every 8-10 actions.
    done: features/files completed so far
    remaining: features/files still to implement
    notes: current state, key decisions, anything to remember on resume
    """
    content = (
        f"CHECKPOINT task-{TASK_ID}\n"
        f"done: {', '.join(done)}\n"
        f"remaining: {', '.join(remaining)}\n"
        f"notes: {notes}"
    )
    await add_memory(content, f"task-{TASK_ID}")
    return f"checkpoint saved — {len(remaining)} items remaining"
```

`TASK_ID` is injected into the tool closure at agent-build time (same pattern as
`update_vikunja_task` already does).

Register in `build_agent()` alongside the other tools.

### System prompt addition (coder-system in Langfuse)

```
MANDATORY: Call save_progress every 8 actions. Do not wait until the end.
Pass what you have completed, what remains, and any important context.
If you find a CHECKPOINT in your prior memory when starting, resume from
where it left off rather than starting over.
```

### Resume logic in `_run_coder`

The existing `search_memory` call at the top of `_run_coder` already fires before the
agent runs. When a checkpoint exists, it appears in `prior_context` and the coder picks
up where it left off. No extra code needed — the system prompt instruction handles it.

### Why this matters for Hatchet retries

Hatchet retries a failed task from the beginning. Without checkpointing, a crash at step
40 means re-cloning the repo, re-implementing features 1-3 that were already committed.
With checkpointing, the retry reads "done: PostList, PostPage, DarkMode — remaining:
ResponsiveLayout" from Mem0 and skips the completed work. The 3 committed features are
already on the branch; the coder only re-does the incomplete one.

---

## 28c — Feature Decomposition Pipeline

**Effort:** ~1 day. **Impact:** Eliminates context exhaustion structurally for multi-feature specs.

### The core insight

A 4-feature spec given to one coder in one `agent.run()` call needs ~80k tokens. The
same 4 features given to 4 sequential coder runs need ~15k tokens each. The model is
identical. The output quality is better because the context is clean.

### Execution model: sequential inline, single Hatchet task

The 4 features do NOT run in parallel and do NOT dispatch separate Hatchet events.
They run as sequential `await` calls inside one Hatchet pipeline task.

```
Hatchet receives: pipeline:feature_decompose    ← one task, one timeout
         │
         ▼
FeaturePipelineWorker (long-running process)
         │
         ├── await build_coder_agent().run("implement PostList")   ← GPU request 1
         │   commits, pushes, saves Mem0 checkpoint
         │
         ├── await build_coder_agent().run("implement PostPage")   ← GPU request 2
         │   commits, pushes, saves Mem0 checkpoint
         │
         ├── await build_coder_agent().run("implement DarkMode")   ← GPU request 3
         │   commits, pushes, saves Mem0 checkpoint
         │
         ├── await build_coder_agent().run("implement Responsive") ← GPU request 4
         │   commits, pushes, saves Mem0 checkpoint
         │
         └── await build_coder_agent().run("open draft PR")        ← GPU request 5
         │
         ▼
Hatchet marks task complete
```

**Why sequential and not parallel:**
- One GPU, one model (Qwen3.6-35B-A3B on the RTX 4000). Parallel requests queue at
  llama.cpp/LiteLLM — no latency benefit, just contention.
- Feature N often imports from feature N-1's files. Sequential execution means each
  coder run clones the branch with all prior features already committed.
- Sequential is simple to reason about and debug.

**Why inline and not separate Hatchet events:**
- Hatchet events for each feature would require the pipeline to poll for completion
  before dispatching the next. Complex and fragile.
- The existing `pipelines/research_then_code.py` uses the same inline pattern — research
  node runs, then coder node runs, all inside one Hatchet task. Same design here.
- If the pipeline worker crashes, Hatchet retries the whole task. Mem0 checkpoints make
  this idempotent (skip already-done features).

### New file: `agents/pipeline/feature_worker.py`

```python
"""Hatchet worker: pipeline:feature_decompose — sequential per-feature coder runs."""
import tomllib
import re
import time
from datetime import timedelta

from hatchet_sdk import Context, Hatchet
from pydantic import BaseModel
from pydantic_ai.usage import UsageLimits

from agents.coder.agent import build_agent as build_coder_agent
from common.memory_tools import add_memory, search_memory
from common.metrics import start_metrics_server, task_invocations, task_active, task_duration

_AGENT_NAME = "feature-pipeline"


class FeaturePipelineInput(BaseModel):
    task_id: int
    task_title: str
    spec_toml: str          # full spec from Phase 27


def _parse_features(spec: dict) -> list[str]:
    return spec.get("app", {}).get("features", [])


def _feature_prompt(spec: dict, feature: str, index: int, total: int, task_id: int) -> str:
    repo      = spec["repos"]["primary"]
    framework = spec.get("app", {}).get("framework", "")
    branch    = f"praetor-coder/task-{task_id}"
    return (
        f"Task #{task_id} — feature {index + 1}/{total}\n"
        f"Repo: {repo}\n"
        f"Branch: {branch} (already exists — clone it, do not create a new branch)\n"
        f"Framework: {framework}\n\n"
        f"Implement this ONE feature completely. Do not stub it.\n"
        f"Feature: {feature}\n\n"
        f"When done: commit all changes with message 'feat: {feature}' and push.\n"
        f"Do NOT open a PR. The pipeline will open the PR after all features are done.\n"
        f"Call save_progress with what you completed."
    )


def _finalize_prompt(spec: dict, features: list[str], task_id: int) -> str:
    repo   = spec["repos"]["primary"]
    branch = f"praetor-coder/task-{task_id}"
    title  = spec["task"]["title"]
    feat_list = "\n".join(f"- {f}" for f in features)
    return (
        f"Task #{task_id} — open draft PR\n"
        f"Repo: {repo}, branch: {branch}\n\n"
        f"All features are implemented and committed. Open a draft PR titled:\n"
        f"'feat: {title}'\n\n"
        f"PR description should list all implemented features:\n{feat_list}\n\n"
        f"Call update_vikunja_task with the PR URL when done."
    )


async def _run_feature_pipeline(input: FeaturePipelineInput, context: Context) -> dict:
    spec     = tomllib.loads(input.spec_toml)
    features = _parse_features(spec)
    task_agent_id = f"task-{input.task_id}"
    request_limit = spec.get("dispatch", {}).get("request_limit", 50)

    task_active.labels(agent=_AGENT_NAME).inc()
    t0 = time.monotonic()
    try:
        for i, feature in enumerate(features):
            # Crash recovery: skip features already committed
            hits = await search_memory(f"feature done: {feature}", task_agent_id)
            if any("done" in h.lower() for h in hits):
                continue

            prompt = _feature_prompt(spec, feature, i, len(features), input.task_id)
            agent  = build_coder_agent()
            await agent.run(prompt, usage_limits=UsageLimits(request_limit=request_limit))
            await add_memory(f"feature done: {feature}", task_agent_id)

        # Finalize: open the PR
        agent = build_coder_agent()
        result = await agent.run(
            _finalize_prompt(spec, features, input.task_id),
            usage_limits=UsageLimits(request_limit=20),
        )
        await add_memory(
            f"task-{input.task_id} ({input.task_title}) complete: {str(result.output)[:300]}",
            "planner-global",
        )
        task_invocations.labels(agent=_AGENT_NAME, status="success").inc()
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

    run_pipeline = hatchet.task(
        name="feature-pipeline",
        on_events=["pipeline:feature_decompose"],
        input_validator=FeaturePipelineInput,
        execution_timeout=timedelta(minutes=90),   # covers N features at ~15 min each
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
```

### Concurrency design — `GROUP_ROUND_ROBIN` not `CANCEL_NEWEST`

`max_runs=1` ensures only one feature pipeline runs at a time. A second dispatch
(e.g., user starts a second "build app" task while the first is running) uses
`GROUP_ROUND_ROBIN` — it queues and waits rather than being cancelled. This is
intentional: one GPU, one model, one inference stream. The second pipeline starts
automatically when the first completes.

The coder worker already uses `CANCEL_NEWEST` on `agent:code`. That's correct for
direct single-task dispatches (cancel the duplicate). For the pipeline, cancelling
is wrong — the second pipeline is a different task, not a duplicate.

### Routing in `webhooks/spec.py` (Phase 27)

```python
elif task_type in ("modify_app", "fix_pr", "modify_pr"):
    features = spec.get("app", {}).get("features") or spec.get("app", {}).get("changes") or []
    if len(features) > 1:
        # Multi-feature: use decomposition pipeline
        dispatch_agent(task_id, spec["task"]["title"], req.spec_toml, "feature_pipeline")
    else:
        # Single feature or fix: direct coder dispatch
        dispatch_agent(task_id, spec["task"]["title"], req.spec_toml, "code")
```

For `new_app` specs, the pipeline is also used if features > 1. The `app_factory` still
handles repo creation and infra provisioning; once provisioning completes it dispatches
`pipeline:feature_decompose` instead of `agent:code`.

### `common/dispatch.py` addition

```python
EVENT_MAP: dict[str, str] = {
    "research":         "agent:research",
    "code":             "agent:code",
    "pipeline":         "pipeline:research_code",
    "scaffold":         "agent:scaffold",
    "feature_pipeline": "pipeline:feature_decompose",   # new
}
```

### Initial branch creation

When `new_app` routes to the feature pipeline, the first feature's coder prompt says
"branch praetor-coder/task-{id} already exists — clone it." But for a brand-new repo,
that branch doesn't exist yet. The feature pipeline worker creates it before the first
feature run:

```python
# Before the feature loop:
if task_type == "new_app":
    setup_agent = build_coder_agent()
    await setup_agent.run(
        f"Repo {repo} was just created from template. "
        f"Clone it, create branch praetor-coder/task-{task_id}, "
        f"and push the empty branch. Do not implement anything yet."
    )
```

This is a tiny agent run (~3 tool calls: clone, checkout -b, push). Sets up the branch
so all feature runs can `git clone --branch praetor-coder/task-{task_id}` reliably.

---

## GPU / concurrency summary

| Question | Answer |
|---|---|
| Do 4 features run at the same time? | No — sequential `await`, one at a time |
| Does the GPU see parallel requests? | No — one completes before next starts |
| How long does the pipeline Hatchet task run? | Total of all feature times (~5 min each + finalize) |
| What if a second pipeline is dispatched while one runs? | Queues via GROUP_ROUND_ROBIN, starts when first finishes |
| What if the worker crashes mid-pipeline? | Hatchet retries; Mem0 checkpoints skip done features |
| How does the model "know which memory to reclaim"? | It doesn't — sequential execution means no concurrent KV caches |

---

## Implementation Checklist

### 28a — Tool truncation (do first, no dependencies)
- [ ] `agents/coder/agent.py`: cap `run_shell` return at 3000 chars (keep tail)
- [ ] `agents/coder/agent.py`: cap `read_file` return at 8000 chars (was 16000)
- [ ] Update `coder-system` prompt in Langfuse: note that outputs are truncated and to
      use targeted reads rather than reading whole files

### 28b — Mem0 checkpointing
- [ ] `agents/coder/agent.py`: add `save_progress(done, remaining, notes)` tool
- [ ] `agents/coder/agent.py`: register `save_progress` in `build_agent()`
- [ ] `coder-system` prompt in Langfuse: "Call save_progress every 8 actions. Mandatory."
- [ ] `coder-system` prompt: "If CHECKPOINT found in prior memory, resume from it."
- [ ] Test: mock agent run that crashes at step 20; verify retry skips done items

### 28c — Feature decomposition pipeline
- [ ] `agents/pipeline/feature_worker.py`: new file (code above)
- [ ] `common/dispatch.py`: add `"feature_pipeline": "pipeline:feature_decompose"`
- [ ] `webhooks/spec.py`: route multi-feature specs to `feature_pipeline`
- [ ] `webhooks/app_factory.py`: `_provision_and_dispatch` dispatches `feature_pipeline`
      instead of `code` when spec has multiple features
- [ ] Register `feature-pipeline-worker` in the main worker entrypoint alongside other workers
- [ ] k3s-dean-gitops: add `feature-pipeline-worker` deployment (separate pod, same image)
- [ ] `coder-system` prompt: add Mode D — "feature step: commit and push only, do not open PR"
- [ ] Test: spec with 3 features → verify 3 sequential agent.run() calls → verify branch has 3 commits

### Tests (`tests/unit/test_phase28_feature_pipeline.py`)
- [ ] `_parse_features` extracts list from spec
- [ ] `_feature_prompt` includes "do not open PR" instruction
- [ ] `_finalize_prompt` includes all features in PR body
- [ ] Crash recovery: mock Mem0 returning "done" for feature 1 → verify feature 1 skipped
- [ ] Single-feature spec routes to `agent:code`, not `pipeline:feature_decompose`
- [ ] Multi-feature spec routes to `pipeline:feature_decompose`
