# Phase 22 — Coder Re-Dispatch Loop

**Goal:** Close the coder→reviewer feedback loop. When the reviewer posts `REQUEST_CHANGES`, automatically re-dispatch the coder with the review feedback attached. Cap at 2 coder attempts before leaving the PR open for human review.

---

## Pre-conditions

- Phase 21 complete (mem0 active, reviewer writes structured memories)
- Reviewer worker live (`amerenda-reviewer` GitHub App posting reviews)
- Coder worker live (praetor-coder GitHub App opening draft PRs)

---

## What Gets Built

### Trigger: reviewer REQUEST_CHANGES → re-dispatch coder

Currently:
```
coder opens draft PR → reviewer posts review → human decides
```

After Phase 22:
```
coder opens draft PR
  → reviewer posts review
    → if REQUEST_CHANGES and attempt < 2: re-dispatch coder with review feedback
    → if APPROVE or attempt >= 2: leave for human review
```

### Implementation

**`webhooks/github_webhook.py`** — add handler for `pull_request_review` events:
- If `action == "submitted"` and `state == "REQUEST_CHANGES"`
- Extract repo, PR number, review body, reviewer comments
- Check attempt counter (stored in PR description or a Vikunja task comment)
- If attempt < 2: dispatch `agent:code` with the original task context + review feedback appended
- If attempt >= 2: post a PR comment noting the loop is exhausted, leave for human

**`agents/coder/agent.py`** — system prompt already does `search_memory` first (Phase 21). Add:
- Accept `review_feedback: str | None` in the dispatch payload
- If present, append it to the task description so the LLM sees what the reviewer said

**Attempt counter:** Simplest approach — store in the PR description as a hidden HTML comment `<!-- praetor-attempt: 1 -->`. The webhook handler reads it, increments, and writes it back on re-dispatch. No new state store needed.

### What "re-dispatch" means

The coder agent pushes a new commit to the same branch (not a new PR). The reviewer will re-review the updated diff via a new `pull_request` → `synchronize` event (already wired in `github_webhook.py`).

---

## Ready Conditions

- Reviewer `REQUEST_CHANGES` on a praetor-coder draft PR triggers automatic coder re-dispatch
- Coder reads the reviewer feedback and mem0 context before making the fix
- After 2 failed attempts, PR stays open with a note — no infinite loop
- `APPROVE` on first review skips the loop entirely

---

## What NOT to Build

- No moderator agent yet (Phase 24)
- No changes to the reviewer — it reviews the same way regardless of attempt number
- No changes to how PRs are opened or merged

---

## Notes

- The attempt counter in the PR description is intentionally low-tech. It avoids needing a new DB table or ConfigMap for transient loop state.
- mem0 is the long-term fix for repeated disputes: if the reviewer writes a memory on attempt 1, the coder reads it on attempt 2 and should apply it correctly.
- The cap of 2 is deliberate. 3+ attempts without resolution almost always means the original task is underspecified, not that the coder needs another try.
