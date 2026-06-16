# Phase 13 — Agent Benchmarking & Eval

**Goal:** Build a systematic quality baseline for all agents. Create eval datasets in Langfuse, automate benchmark runs via Hatchet, and establish pass/fail thresholds that block bad prompt changes before they reach production.

## Pre-conditions

- Phase 12 complete (all agents verified healthy, smoke tests green)
- Langfuse deployed and all agents already instrumented with `@observe()` (Phase 9)
- `POST /api/v1/dispatch` live (Phase 14 dispatch)

## What Gets Built

### 13a — Eval Datasets in Langfuse

Create one dataset per agent type in the Langfuse UI (or via API). Each dataset contains 5–10 representative tasks with known-good expected outputs.

**Research agent dataset (`research-eval`):**
```
item 1: input="Research Tailscale exit node ACL interaction"
        expected_output_contains=["exit node", "ACL", "subnet"]
item 2: input="Explain Hatchet durable execution model"
        expected_output_contains=["durable", "retry", "workflow"]
...
```

**Coder agent dataset (`coder-eval`):**
```
item 1: input="Add /healthz endpoint to a FastAPI app"
        expected: PR opened, diff contains '/healthz', test file present
item 2: input="Add pagination to GET /api/v1/tasks"
        expected: PR opened, diff contains 'limit' and 'offset'
...
```

**Reviewer agent dataset (`reviewer-eval`):**
```
item 1: PR diff with an obvious SQL injection vulnerability
        expected_review_mentions=["injection", "parameterized", "unsafe"]
item 2: PR diff that is clean and well-structured
        expected: review is positive, no blocking comments
...
```

Datasets are created once and live in Langfuse permanently. Runs append new trace results without modifying the dataset.

---

### 13b — Benchmark Hatchet Event

New Hatchet event: `agent:benchmark`. The benchmark worker picks up the event, runs the target agent against a single dataset item, scores the output, and writes the score back to Langfuse.

**Input:**
```python
class BenchmarkInput(BaseModel):
    dataset_name: str      # "research-eval"
    dataset_item_id: str   # Langfuse dataset item ID
    agent_type: str        # "research" | "code" | "review"
    model: str             # "qwen3-35b" (or any LiteLLM model alias)
    prompt_version: str    # Langfuse prompt name + version, e.g. "coder-system:v4"
```

**Worker behavior (`agents/benchmark/worker.py`):**
1. Fetch dataset item from Langfuse API
2. Dispatch agent via `common/dispatch.py:dispatch_agent()` with item's input
3. Wait for agent completion (poll Hatchet run status, timeout 10 min)
4. Fetch agent's Langfuse trace
5. Score the trace output against the expected criteria using a lightweight LLM judge call
6. Write score back to Langfuse via `POST /api/public/scores`

**Scoring — LLM judge (no custom eval framework):**
```python
judge_prompt = f"""
Score the following agent output from 0.0 to 1.0.
Expected criteria: {item.expected_criteria}
Agent output: {agent_output}
Return only a JSON object: {{"score": <float>, "reason": "<one sentence>"}}
"""
# Use qwen3-35b via LiteLLM — same model, cheap judge call
score_resp = litellm.completion(model="qwen3-35b", messages=[{"role":"user","content":judge_prompt}])
```

---

### 13c — Benchmark Runner Script

A CLI script (not a UI — that's Phase 15) that dispatches a full benchmark suite and prints results:

```bash
# Run full research-eval suite against production prompt version
python scripts/run_benchmark.py \
  --dataset research-eval \
  --agent-type research \
  --model qwen3-35b \
  --prompt-version coder-system:v4
```

Output:
```
Running benchmark: research-eval (10 items) × qwen3-35b × coder-system:v4
[1/10] Research Tailscale ACLs ........... 0.92 ✓
[2/10] Explain Hatchet durable execution .. 0.88 ✓
...
Suite complete. Mean score: 0.87. Langfuse experiment: research-eval-2026-06-20
```

Results are saved as a Langfuse "experiment" — viewable at `langfuse.amer.dev`.

---

### 13d — Baseline Establishment

Run the benchmark suite once against current production prompts. Record the baseline scores in `docs/eval-baselines.md`:

```markdown
| Dataset | Model | Prompt | Date | Mean Score | Min Score |
|---------|-------|--------|------|-----------|-----------|
| research-eval | qwen3-35b | coder-system:v4 | 2026-06-XX | 0.87 | 0.72 |
| coder-eval    | qwen3-35b | coder-system:v4 | 2026-06-XX | 0.81 | 0.65 |
```

Any future prompt change must be run through the benchmark suite before promoting to production. If mean score drops more than 0.05 below baseline, the change is rejected.

---

### 13e — Benchmark Worker Deployment

Same pattern as other workers. Add to `praetor` CI image matrix.

```dockerfile
# Dockerfile.benchmark-worker — shares base with coder-worker
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-m", "agents.benchmark.worker"]
```

k3s deployment: same env vars as other workers (Hatchet, LiteLLM, Langfuse, Mem0).

## Phase 13 Ready Conditions

1. `research-eval`, `coder-eval`, and `reviewer-eval` datasets exist in Langfuse with ≥5 items each
2. `agent:benchmark` Hatchet event dispatches benchmark worker successfully
3. `python scripts/run_benchmark.py --dataset research-eval` completes and writes scores to Langfuse
4. Baseline scores recorded in `docs/eval-baselines.md`
5. `benchmark-worker` pod Running in k3s praetor namespace
6. Langfuse experiment view shows scored run history for each dataset
