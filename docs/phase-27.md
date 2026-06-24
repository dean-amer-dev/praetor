# Phase 27 — Inline Arbitration

**Goal:** When the Phase 21 coder re-dispatch loop exhausts its attempts (2 failed tries, reviewer still `REQUEST_CHANGES`), fire a focused inline LLM call that reads both sides, does a targeted web search on the specific dispute, and writes a decision memo to mem0 and the PR. No new agent process — inline, like the pre-PR review loop in Phase 21.

---

## Pre-conditions

- Phase 21 complete (coder re-dispatch loop live, attempt counter in place)
- The dispute scenario exists in the wild (loop has been seen exhausting in practice)

---

## What Gets Built

### Trigger

Fires from `github_webhook.py` when:
1. `pull_request_review` event with `state == "REQUEST_CHANGES"`
2. Attempt counter == 2 (loop exhausted — this is the third reviewer rejection)

### Inline arbitration call

A single LiteLLM call (same pattern as `_call_review_llm` in `mcp_factory.py`, not a full agent dispatch). Takes:
- The PR diff
- The reviewer's last `REQUEST_CHANGES` comment (the specific issues raised)
- The coder's last commit message + changed files

System prompt instructs the LLM to:
1. Identify the specific technical dispute (e.g. "reviewer wants ConfigMap, coder used Secret")
2. Do a targeted web search via LiteLLM MCP tools (`lm_web_search`) on the dispute
3. Return a structured decision: `recommended_approach`, `rationale`, `relevant_links`

This is NOT a full research agent run — it's one focused call with a tight budget (max 3 tool calls).

### Outputs

**PR comment** (posted via `amerenda-reviewer` app):
```
> 🏛️ **Cicero Arbiter** — loop exhausted after 2 attempts

## Dispute
<what the reviewer and coder disagreed on>

## Recommendation
<recommended_approach + rationale + links>

## Decision memo
Stored in mem0 under `reviewer-{repo}` so future runs don't repeat this dispute.
Human review required to merge.
```

**mem0 write** — structured entry scoped to `reviewer-{repo}`:
```
pattern: <dispute type>
example: <repo> PR #<number>
resolution: <recommended_approach>
context: <rationale summary>
```

### What the arbitration does NOT do

- Does not re-dispatch coder again — the PR stays open for human review after arbitration
- Does not override the reviewer's verdict — the PR still has `REQUEST_CHANGES`
- Does not run the full research pipeline — one LLM call, capped tool budget
- Does not auto-merge even if the recommendation is clear

---

## Ready Conditions

- Arbitration fires after attempt 2 exhaustion, not before
- PR comment identifies the dispute and gives a recommendation with sources
- Decision written to mem0 so the next coder run on a similar task finds it immediately
- Human still has final say — the PR stays in `REQUEST_CHANGES` state

---

## Notes

- The goal is to break the coder-reviewer loop with *information*, not with authority. The arbitration adds context the coder didn't have; it doesn't force an outcome.
- If arbitration repeatedly recommends the same thing but the coder keeps ignoring it, that's a prompt quality problem, not an architecture problem.
- Prefer repo-specific preferences (gitops, containers, stateless, mac-mini-m4 for DB, version pinning) as standing context in the arbitration system prompt so recommendations stay consistent with the platform.
- The Langfuse prompt name for the arbitration system prompt: `arbitration-system`.
