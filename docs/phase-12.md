# Phase 12 — Platform Verification & Hardening

**Goal:** Confirm every deployed agent is healthy end-to-end before adding new capabilities. Close out all 🟡 Verify statuses (Phase 4 mem0, Phase 7 PR reviewer + QA). All smoke and E2E tests green.

This phase has no new code. It is a verification gate. Nothing from Phase 13 onward begins until this phase is complete.

## Pre-conditions

- Phases 0–11 complete (conversational dispatch live via Phase 11)
- All workers deployed (research, coder, qa, reviewer, pipeline, scaffold)
- `POST /api/v1/dispatch` live and tested

## Work Items

### 12a — Verify mem0 (close Phase 4)

mem0 was migrated to the official arm64 image (k3s-dean-gitops #757). Confirm it is actually working, not just Running.

**Steps:**
1. `curl https://mem0.amer.dev/healthz` → 200
2. Add a memory via `POST /memories` → retrieve it via `GET /memories?agent_id=test`
3. Trigger a research run → confirm agent writes to mem0 (check Langfuse trace for `add_memory` tool call)
4. Trigger a second research run on the same topic → confirm agent reads prior memory (search_memory tool call in trace)

**Done conditions:**
- [ ] `curl https://mem0.amer.dev/healthz` returns 200
- [ ] Manual write+read round-trip succeeds
- [ ] Research agent trace shows both `add_memory` and `search_memory` tool calls on back-to-back runs

---

### 12b — Verify PR reviewer and QA workers (close Phase 7)

Workers were built and images pushed, but end-to-end flow was never confirmed with a live PR.

**Steps:**
1. Confirm both pods are Running: `kubectl get pods -n praetor | grep -E 'reviewer|qa'`
2. Open a test PR on `amerenda/praetor` → confirm `amerenda-reviewer[bot]` posts a review comment within 5 minutes
3. Check Langfuse for a reviewer trace tied to the PR
4. Check Hatchet UI for the `agent:review` run — status should be Succeeded

**Done conditions:**
- [ ] `praetor-qa-worker` and `praetor-reviewer-worker` pods both Running
- [ ] Test PR receives a review comment from `amerenda-reviewer[bot]`
- [ ] Hatchet run for the PR review shows Succeeded
- [ ] Langfuse trace exists for the reviewer run

---

### 12c — Full E2E smoke pass

Run the full test suite against the live cluster. Every agent path must be exercised.

```bash
# Run from praetor repo root against live cluster
PRAETOR_BASE_URL=https://praetor.amer.dev \
PRAETOR_API_KEY=$PRAETOR_API_KEY \
pytest tests/smoke/ tests/e2e/ -v --timeout=300
```

**Agents to verify:**
| Agent | Trigger | Expected output |
|-------|---------|----------------|
| research | `POST /api/v1/dispatch` type=research | mem0 entry written, Langfuse trace |
| coder | `POST /api/v1/dispatch` type=code | draft PR opened on test repo |
| pipeline | `POST /api/v1/dispatch` type=pipeline | both research + code steps traced |
| reviewer | GitHub PR webhook | review comment posted |
| scaffold | `POST /api/v1/dispatch` type=scaffold | draft PR on amerenda/praetor or dean-mcp |

**Done conditions:**
- [ ] All smoke tests pass
- [ ] All five E2E paths produce expected outputs
- [ ] No CrashLoopBackOff, OOMKilled, or ImagePullBackOff across any praetor pod
- [ ] All praetor ArgoCD apps: Synced + Healthy

---

### 12d — Health Dashboard Bookmark

After verification, create a single-line curl that can be run any time to confirm the platform is up:

```bash
# Paste this as a manual_run or alias — platform health check
for url in \
  https://praetor.amer.dev/healthz \
  https://mem0.amer.dev/healthz \
  https://litellm.amer.dev/health \
  https://hatchet.amer.dev/api/v1/healthz \
  https://langfuse.amer.dev/api/public/health; do
  code=$(curl -sf -o /dev/null -w "%{http_code}" "$url")
  echo "$code  $url"
done
```

Store as `manual_runs/platform-health-check.sh`. This becomes the go-to "is everything up?" command before starting any phase.

**Done conditions:**
- [ ] `manual_runs/platform-health-check.sh` committed and all URLs return 200

## Phase 12 Ready Conditions

1. Phase 4 fully verified: mem0 healthz + memory write/read + research agent using memory across runs
2. Phase 7 fully verified: reviewer and QA pods Running + test PR receives review comment
3. All smoke and E2E tests pass (no skips, no failures)
4. Health check script committed and all 5 URLs return 200
5. No praetor pods in error state
