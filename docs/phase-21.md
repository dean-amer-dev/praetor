# Phase 21 — Mem0 Integration + Pre-PR Review Loop

**Goal:** Make mem0 a real first-class layer. Agents check memory before acting and write structured memories after. The MCP factory reviews its own output before opening a PR. No post-PR auto-fix loop.

---

## Pre-conditions

- Phase 18 complete (MCP factory stable, mcp/register live)
- mem0 server running (`mem0` namespace, pgvector on mac-mini)
- `common/memory_tools.py` exists with `add_memory` / `search_memory` wrappers

---

## What Gets Built

### 21a — Pre-PR Review Loop in `mcp_factory.py`

Currently `register_mcp()` generates YAML → creates branch → opens PR in one shot.

**New flow:**

```
generate YAML strings (no GitHub writes yet)
  ↓
inline LLM review — pass all YAML as context, ask for structured feedback
  ↓
issues found? → apply fixes to YAML strings, loop (max 3 iterations)
  ↓
approved OR iterations exhausted → create branch + files + open PR
  (if exhausted: PR description includes ⚠️ warning + review notes)
```

**Implementation notes:**

- The LLM call is direct to LiteLLM (`/v1/chat/completions`), same as `mcp_request.py` does today — no Hatchet, no new worker
- Reviewer prompt is tuned specifically for Kubernetes manifests: probe types, RBAC correctness, image policy, resource limits
- Fix application is also an LLM call: pass the YAML + the issues, get back corrected YAML
- Loop state is in-memory only — no Hatchet steps, no GitHub commits until the loop exits
- Max 3 iterations is a hard cap; on exhaustion the PR opens with a `⚠️ auto-review: N issues unresolved` note in the body
- This prompt lives in Langfuse (`mcp-pre-review-system`) so it can be tuned from the Control Plane UI (Phase 20) without a deploy

**New model field:**

```python
class McpRegistration(BaseModel):
    ...
    skip_pre_review: bool = False  # escape hatch for tests / manual calls
```

---

### 21b — Reviewer Agent: Check-Before-Write Mem0

The reviewer agent currently ignores mem0 entirely.

**New behavior:**

1. On every review run, before posting to GitHub: `search_memory(query=<issue description>, agent_id="reviewer")`
2. If a matching memory exists (cosine similarity > threshold): reference it in the review comment ("this pattern was flagged in prior reviews — see memory")
3. If no match: post the review, then `add_memory(content=<structured summary>, agent_id="reviewer")`

**Memory format (reviewer writes this):**

```
pattern: <short description of the issue type>
example: <what triggered it>
resolution: <what the fix was, or "unresolved">
context: <repo/file type where this applies>
```

**Why check-before-write matters:** mem0 deduplication is embedding-based, not exact-match. Without an explicit check, the reviewer will write a near-duplicate memory on every similar PR. The explicit search + conditional write keeps the memory store clean.

**Prompt addition (reviewer system prompt):**

```
Before posting a review finding, call search_memory with the issue description.
If a similar memory exists, reference it and note whether the pattern recurs.
If no match is found, after posting your review call add_memory with a structured
summary of the finding (pattern, example, resolution, context).
Do not write a memory for issues that were already in the existing memory store.
```

---

### 21c — Coder Agent: Read Memory Before Acting

Currently the coder system prompt says "store key decisions in memory" — passive and optional.

**New behavior:** Mandatory first step.

**Prompt change (coder system prompt, step 1):**

```
Before writing any code or reading any files, call search_memory with a
description of the task. Review any relevant memories — they may contain
prior decisions, known pitfalls, or patterns from similar tasks in this repo.
```

After completing a task, the coder already has a soft instruction to store decisions. Strengthen it:

```
After pushing the branch, call add_memory with: what was implemented, any
non-obvious decisions made, and any patterns worth reusing. Use
agent_id='coder-{repo}' so memories are scoped to the repo.
```

**agent_id scoping:** `coder-k3s-dean-gitops`, `coder-praetor`, `coder-dean-mcp` — keeps memories from cross-contaminating across repos with different conventions.

---

### 21d — Research Agent: Read Memory Before Searching

Currently the research agent has `search_memory` available but no instruction to call it first.

**Prompt addition (research system prompt, step 1):**

```
Before searching the web, call search_memory with the research topic.
If prior findings exist, use them as a starting point and focus new searches
on gaps. After completing research, call add_memory with key findings and
their source.
```

This makes the research agent useful across repeated questions about the same domain (Kubernetes, llama.cpp config, etc.) without redundant web searches.

---

### 21e — Reviewer Reads Mem0 on Errors / Known Patterns

Beyond writing memories, the reviewer should actively use them to give better feedback.

**Behavior:** When the reviewer identifies an issue, it searches mem0 for that issue type before writing the review comment. If found, it adds context: "This has appeared in N prior reviews. Prior resolution: X."

This is the same check-before-write flow from 21b, just making explicit that the *read* path is as important as the write path — the reviewer becomes more useful over time, not just a stateless LLM call.

---

## What Does NOT Get Built

- **Post-PR auto-fix loop** — the reviewer and coder share the same underlying model. Reviewer flags an issue → coder "fixes" it → reviewer finds the next issue → infinite loop with no convergence guarantee. The human review IS the post-PR review. Don't automate it away.
- **Cross-agent shared memory namespace** — each agent writes to its own `agent_id` scope. Cross-pollination (e.g., reviewer memories informing coder) happens only through the pre-PR review prompt reading Langfuse-stored patterns, not through a shared mem0 namespace that becomes a garbage dump.

---

## Prompt Tuning Surface

All new prompts (pre-PR review, reviewer memory instructions) are stored in Langfuse, not hardcoded. This means they're editable from the Control Plane UI (Phase 20) without a redeploy. The agents already fetch their system prompts from Langfuse at startup via `common/langfuse_tools.py`.

---

## Phase 21 Ready Conditions

| # | Condition | Status |
|---|-----------|--------|
| 1 | `POST /api/v1/mcp/register` — pre-review loop runs before any GitHub write; Langfuse trace shows review iterations | ✅ |
| 2 | A manifest with a known bad pattern (e.g., `httpGet /health` on a server with no `/health`) is caught and fixed before the PR opens | ✅ |
| 3 | After 3 failed iterations, PR opens with `⚠️ auto-review: N issues unresolved` in the description | ✅ |
| 4 | Reviewer agent: on a second PR with the same issue type, the review comment references the prior memory | ✅ |
| 5 | Reviewer agent: does not write a duplicate memory when the issue is already stored | ✅ |
| 6 | Coder agent: Langfuse trace shows `search_memory` call as the first tool call on every run | ✅ |
| 7 | Research agent: on a repeated topic, Langfuse trace shows `search_memory` hit before any web search | ✅ |
| 8 | All agent memories are scoped by `agent_id` — no cross-contamination between repos or agent types | ✅ |
| 9 | Pre-review prompt is editable in Langfuse (`mcp-pre-review-system`) without a redeploy | ✅ |

**Phase 21 completed 2026-06-22.** PRs shipped: #107 (coder prompt + research 403 fix), #108 (worker pre/post-call memory guarantees), #109 (Vikunja 404 + SSL error handling), #110 (async `search_memory`/`add_memory` for correct Langfuse span propagation).

### Implementation notes

- **Coder memory**: `search_memory` is called by the worker before `agent.run()` and its result injected into the prompt. Worker also post-calls `add_memory` after each run to guarantee storage even if the model skips it. Memory scoped to `coder-{owner}/{repo}`.
- **Research memory**: same pre/post-call pattern. Subprocess receives `prior_context` in stdin and is instructed to confirm via an explicit `search_memory` call. Worker's pre-call appears as the first child span in Langfuse.
- **Langfuse span propagation**: `search_memory` and `add_memory` are `async def` functions wrapping blocking HTTP via `asyncio.to_thread()`. Awaiting them directly (not via `run_in_executor`) keeps them in the async trace context, so `@observe()` creates proper child spans visible in every agent trace.
