# Phase 26 — Inline Arbitration

## You are implementing Phase 26 of the Praetor platform.

**You are allowed to merge PRs for this session.**

---

## Context

You are working on the `amerenda/praetor` monorepo. The platform runs on a k3s cluster (GitOps via `amerenda/k3s-dean-gitops`, synced by ArgoCD). Secrets come exclusively from Bitwarden Secrets Manager (BWS) — never hardcode secrets, never put them in Git.

### What already exists

- **Phase 21** — coder re-dispatch loop: reviewer posts `REQUEST_CHANGES` → coder is re-dispatched with `attempt:` counter. The loop is capped at `attempt < 1` (two coder attempts total).
- When the loop exhausts (reviewer still requests changes after attempt 1), the PR sits open with `REQUEST_CHANGES` and no further action occurs. A human must break the deadlock manually.
- `webhooks/github.py` handles all PR review webhook events
- `_call_review_llm` in `webhooks/mcp_factory.py` is the established pattern for inline LLM calls (single synchronous LiteLLM call, not a full agent dispatch)

### What is missing

When the coder-reviewer loop exhausts, nothing surfaces a recommendation. The PR just stalls. Phase 26 adds an inline arbitration step that fires on loop exhaustion, analyzes the dispute, and posts a structured recommendation memo to the PR comment and mem0.

---

## Your constraints

**GitOps only.** Every change to running infrastructure must go through a PR on `k3s-dean-gitops`. No `kubectl apply`, no direct pod edits, no one-off patches.

**BWS is the single source of truth for all secrets.** No new secrets needed for this phase — it uses the existing `LITELLM_API_KEY` and `PRAETOR_API_KEY` already wired into the webhook-adapter pod.

**Ansible-playbooks for infrastructure only.** This phase is pure application code — no infrastructure changes needed.

**This phase adds no new Hatchet workers and no new k3s deployments.** Arbitration is an inline LLM call inside the existing webhook-adapter, identical in pattern to `_call_review_llm` in `mcp_factory.py`.

---

## What to build

### 1. Trigger in `webhooks/github.py`

In the `pull_request_review` handler, add a branch after the existing re-dispatch logic:

```
if state == "REQUEST_CHANGES":
    attempt = parse_attempt_from_pr_body(pr)
    if attempt >= 1:
        # Loop exhausted — run arbitration instead of re-dispatching coder
        await _run_arbitration(pr, review_body, token)
    else:
        # Existing re-dispatch logic
        ...
```

### 2. `_run_arbitration()` in `webhooks/github.py`

A single async function. Takes the PR metadata, the reviewer's last comment, and the coder's last commit message. Makes one LiteLLM call with a Langfuse-managed system prompt (`arbitration-system`). Falls back to an inline default if the Langfuse prompt is missing.

**LiteLLM call inputs:**
- PR diff (fetched via GitHub API — first 4000 chars)
- Reviewer's REQUEST_CHANGES comment body
- Coder's last commit message + changed file list

**System prompt instructions:**
1. Identify the specific technical dispute (e.g. "reviewer wants ConfigMap, coder used Secret")
2. Call `lm_web_search` (via LiteLLM MCP tools) on the dispute — max 3 tool calls total
3. Return structured JSON: `{ dispute, recommended_approach, rationale, relevant_links }`

Cap: `max_tokens=1024`, `temperature=0`. This is a focused decision call, not an open-ended research run.

### 3. Outputs from `_run_arbitration()`

**PR comment** (posted via `amerenda-reviewer` GitHub App — same auth pattern as existing reviewer posts):

```
> 🏛️ **Cicero Arbiter** — re-dispatch loop exhausted after 2 attempts

## Dispute
{dispute}

## Recommendation
{recommended_approach}

{rationale}

**Sources:** {relevant_links}

---
*Decision memo written to mem0. Human review required to merge.*
```

**Mem0 write** — scoped to `reviewer-{repo}`, structured entry:
```
pattern: {dispute type}
example: {repo} PR #{number}
resolution: {recommended_approach}
context: {rationale summary}
```

Use the existing `_add_memory` pattern from any agent worker — call the mem0 API directly with the structured entry.

### 4. Langfuse prompt

Create `arbitration-system` prompt in Langfuse (label: `production`). Content: the system prompt described above. The arbitration code fetches it via `get_system_prompt("arbitration-system", "production")` with the inline default as fallback — same pattern as `_PRE_REVIEW_SYSTEM_FALLBACK` in `mcp_factory.py`.

### 5. Unit tests

Add tests in `tests/unit/test_arbitration.py`:
- Trigger condition: fires at attempt >= 1, not before
- Output structure: PR comment contains all three sections
- Mem0 write: correct namespace and fields

---

## Deployment

This is a code-only change to the webhook-adapter. Open a PR on `amerenda/praetor`. CI will build the image and open a deploy PR on `k3s-dean-gitops`. Merge the deploy PR.

---

## Done when

1. A test PR that has been through two coder attempts (both rejected) causes the arbitration comment to appear, posted by `amerenda-reviewer[bot]`
2. The comment contains: the dispute summary, a recommendation, and at least one source link from web search
3. A mem0 entry exists under `reviewer-{repo}` with `pattern` and `resolution` fields
4. The PR is NOT re-dispatched to coder — it stays in `REQUEST_CHANGES` state with only the arbitration comment added
5. Webhook-adapter pod in k3s is `Running` with the new image
6. ArgoCD shows the `praetor` application as `Healthy` and `Synced`
