# Phase 26 — Skills Benchmark

**Goal:** Measure whether a skill actually changes agent behavior in the intended direction. For each skill, run a small eval set twice — with and without the skill injected — and record the compliance delta in Langfuse. Skills that don't move the needle get flagged; skills that work get a score attached. Later, Phase 30 (Model Factory) consumes this infrastructure to report how well each model responds to skill injection.

---

## Pre-conditions

- Phase 24 complete (skills API live, `assemble_prompt` running in all workers)
- Phase 14 complete (benchmark-worker deployed, Langfuse eval datasets + scores working)
- `benchmark:skill` Hatchet event does not conflict with existing `benchmark:*` events

---

## The Core Insight

Academic benchmarks (SWE-bench, BFCL, AgentBench) measure *model capability* on fixed tasks. They do not measure what we care about: **does injecting this skill snippet change agent behavior in the intended direction?**

These are orthogonal questions. A model that scores 74% on SWE-bench may completely ignore a skill instruction. Skill benchmarking is a behavioral compliance measurement — it requires before/after comparison on tasks specifically chosen to exercise the skill.

---

## What Gets Built

### Eval dataset storage

Each skill optionally has an associated eval dataset in Langfuse: `skill-eval-{name}`.

A dataset item is:

```json
{
  "input": {
    "task_description": "Write a Python function that validates an email address.",
    "expected_behavior": "Creates a new .py file starting with '# created by praetor-coder'"
  },
  "expected_output": {
    "rubric": "file_starts_with_header",
    "passing_condition": "The output file contains the comment '# created by praetor-coder' as the first line."
  }
}
```

5–8 items per skill. Items are authored once when the skill is created and stored in Langfuse. The dataset is reused every time the benchmark runs.

### New API endpoints

```
POST /api/v1/skills/{name}/eval-dataset
     body: { "items": [ { "input": {...}, "expected_output": {...} } ] }
     → creates/replaces Langfuse dataset skill-eval-{name}

GET  /api/v1/skills/{name}/eval-dataset
     → returns current dataset items

POST /api/v1/skills/{name}/benchmark
     body: { "agent_name": "coder" }   # which agent to test
     → dispatches benchmark:skill event to benchmark-worker
     → returns { "run_id": "...", "status": "running" }

GET  /api/v1/skills/{name}/benchmark/{run_id}
     → returns run status + scores when complete
```

### benchmark-worker: `benchmark:skill` event

New Hatchet task in the existing benchmark-worker. On receipt:

```
1. Fetch eval dataset skill-eval-{name} from Langfuse
2. FOR EACH item in dataset:
   a. Dispatch task to agent (WITHOUT skill): record result
   b. Dispatch task to agent (WITH skill assigned): record result
   c. Score both results against item's rubric using LLM-as-judge
   d. Write scores to Langfuse as eval observations on the dataset run

3. Compute three aggregate scores:
   - compliance_rate:   fraction of items where WITH-skill passes and WITHOUT-skill fails
   - quality_delta:     mean(score_with) - mean(score_without)  (can be negative)
   - regression_rate:   fraction of unrelated items (sampled from existing eval) that degrade

4. Write benchmark report to Langfuse trace tagged skill-benchmark
5. Write to Mem0 under "skill-effectiveness" namespace:
   { skill: name, agent: agent_name, compliance_rate, quality_delta, regression_rate, run_date }
6. Update skill record in PostgreSQL with latest scores
```

The without-skill run temporarily removes the skill assignment for that agent, dispatches the tasks, then restores it. This ensures the same production worker handles both runs.

### Scoring: LLM-as-judge

Each eval item has a `passing_condition` — a natural language rubric. The judge is a direct LLM call (not an agent loop) using the same `qwen3-35b` model:

```python
judge_prompt = f"""
Did the agent's output satisfy this condition?
Condition: {item['passing_condition']}
Output: {agent_result}
Answer with exactly: PASS or FAIL, then one sentence of explanation.
"""
```

Score per item: 1.0 for PASS, 0.0 for FAIL. Simple, auditable, no partial credit.

### OWU tool additions

Two new methods added to `praetor_dispatch`:

```python
def add_skill_eval_dataset(self, skill_name: str, items: list[dict]) -> str:
    """Attach an eval dataset to a skill. Each item needs 'task_description',
    'expected_behavior', and 'passing_condition'."""

def benchmark_skill(self, skill_name: str, agent_name: str = "coder") -> str:
    """Run the before/after compliance benchmark for a skill. Takes ~5 minutes.
    Returns a run_id — check Langfuse for the full report."""
```

`register_owui_tool.py` updated with both methods.

### Benchmark report format (Langfuse)

Trace name: `skill-benchmark-{name}-{agent_name}`
Tags: `["skill-benchmark", name, agent_name]`

```
Skill: write-tests  Agent: coder  Model: qwen3-35b  Date: 2026-06-25

WITHOUT skill (baseline):
  Item 1: FAIL — no tests written
  Item 2: FAIL — no tests written
  Item 3: PASS — tests included by coincidence
  Baseline compliance: 1/3 = 0.33

WITH skill injected:
  Item 1: PASS — pytest tests added
  Item 2: PASS — pytest tests added
  Item 3: PASS — tests included
  Skill compliance: 3/3 = 1.00

compliance_rate:  0.67  (items that flipped from FAIL→PASS)
quality_delta:   +0.67  (mean score improved)
regression_rate:  0.00  (unrelated tasks unaffected)

Verdict: EFFECTIVE ✓
```

Verdicts:
- `EFFECTIVE` — compliance_rate ≥ 0.5 and regression_rate < 0.2
- `MARGINAL` — compliance_rate 0.2–0.5
- `INEFFECTIVE` — compliance_rate < 0.2
- `HARMFUL` — regression_rate ≥ 0.2 (skill hurts other tasks)

### Auto-trigger on skill creation

When `POST /api/v1/skills` is called and the body includes `eval_items`, the endpoint:
1. Creates the skill and saves to PostgreSQL + Langfuse
2. Creates the eval dataset `skill-eval-{name}`
3. Queues a `benchmark:skill` run automatically

If no `eval_items` are provided, the skill is created with no eval dataset and benchmark must be triggered manually.

### Relationship to Phase 30 (Model Factory)

Phase 26 benchmarks skill effectiveness on the *current production model*.

Phase 30 extends this by also running skill evals *per model* during model validation:

```
For each new model:
  → run existing tool calling / approval / capability benchmarks  (Phase 30 solo)
  → run skill-eval-* datasets for all active skills               (Phase 26 infrastructure)
  → report: skill receptiveness score per model
```

A model that scores well on tool calling but ignores skill instructions is a poor fit for a skill-heavy deployment. The Phase 26 infrastructure is the enabling dependency for this column in the model benchmark report.

---

## What Does NOT Get Built

- **Automatic skill improvement** — the benchmark reports but does not rewrite the skill prompt. Prompt iteration remains manual.
- **Cross-agent skill comparison** — benchmark runs are per-agent. Comparing the same skill across coder/research/reviewer is done by running twice, not automated.
- **Regression CI gate** — adding a skill to an agent does not block deployment if it fails the benchmark. The score is informational in this phase.

---

## Ready Conditions

1. `POST /api/v1/skills/write-tests/eval-dataset` creates a Langfuse dataset with the submitted items
2. `POST /api/v1/skills/write-tests/benchmark` dispatches `benchmark:skill` and returns a `run_id`
3. Benchmark run completes: Langfuse shows a trace tagged `skill-benchmark` with PASS/FAIL per item
4. Three aggregate scores (`compliance_rate`, `quality_delta`, `regression_rate`) written to Mem0 under `skill-effectiveness`
5. `benchmark_skill("write-tests", "coder")` callable from OWU — score visible in Langfuse within 5 minutes
6. A skill with `passing_condition` that the agent never satisfies scores `compliance_rate=0.0` and verdict `INEFFECTIVE`
7. Benchmark run for a skill with no eval dataset returns 404 with a clear message
