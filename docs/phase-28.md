# Phase 28 — Control Plane UI

**Goal:** A purpose-built React dashboard at `praetor.amer.dev` for platform operations — trigger agents, monitor live runs, watch OpenHands sessions, inspect dispatch state, edit prompts, run benchmarks, manage MCPs, and scaffold new components. Replaces tab-switching between Hatchet, Langfuse, and OpenWebUI for routine platform tasks.

## Pre-conditions

- Phase 27 complete (voice dispatch working — all dispatch paths confirmed stable)
- `POST /api/v1/dispatch` and `GET /api/v1/status/{task_id}` live
- Benchmark runner from Phase 14 working (UI wraps existing backend)
- Scaffold worker from Phase 11 working (UI wraps existing `agent:scaffold` event)
- MCP factory from Phase 15 working (UI wraps `POST /api/v1/mcp/register`)
- Phase 12 health check passing (platform must be fully verified before adding UI complexity)

## Design Principles

This is a **control plane**, not a chat interface. OpenWebUI (`bot.amer.dev`) stays for conversation. Praetor UI (`praetor.amer.dev`) is for platform operations only.

Do not re-implement what Hatchet or Langfuse already do well. Link out to them for deep drill-downs.

**Status and health data comes from direct REST API calls — never via MCP.** MCP is for LLM-to-tool calls. The UI hits the praetor API (and thin proxy endpoints) over HTTP directly, the same as any dashboard would.

## Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Frontend | React + Vite | Already in stack (ecdysis uses React) |
| Backend | FastAPI | Already in `praetor/webhooks/app.py` — extend with new router |
| Auth | Existing amer.dev SSO | No new auth infra |

New code lives in:
- `praetor/ui/` — React app (built by CI → nginx serves static assets)
- `praetor/webhooks/ui_api.py` — new FastAPI router for UI-specific endpoints, mounted on existing app

## Feature Areas

### Panel 1: Run Dashboard

Unified live view of recent agent runs across Hatchet + Langfuse.

```
Last 20 runs                                          [auto-refresh: 10s]
┌──────────┬──────────────────────────┬──────────┬──────────┬───────────┐
│ Agent    │ Task                     │ Status   │ Duration │ Trace     │
├──────────┼──────────────────────────┼──────────┼──────────┼───────────┤
│ openhands│ Implement auth refresh   │ ● Live   │ 4m 12s   │ [Watch]   │
│ coder    │ Fix null pointer #1233   │ ✓ Done   │ 8m 45s   │ [View]    │
│ research │ Tailscale ACL research   │ ✓ Done   │ 3m 02s   │ [View]    │
│ reviewer │ PR #88 review            │ ✗ Failed │ 1m 02s   │ [View]    │
└──────────┴──────────────────────────┴──────────┴──────────┴───────────┘
```

Backend: `GET /api/ui/runs` — calls Hatchet API for recent workflow runs, merges with Langfuse trace IDs. "View" links go to `hatchet.amer.dev` or `langfuse.amer.dev`. "Watch" on live runs opens the OpenHands live view (Panel 2) or dispatch stream (Panel 3).

Auto-refreshes every 10s.

---

### Panel 2: OpenHands Live View

When an `openhands` task is running, show what the agent is actually doing.

```
OpenHands — task #1234 "Implement auth refresh"              [4m 12s]
────────────────────────────────────────────────────────────────────────
🔧 read_file: src/auth/token.py
📝 edit_file: src/auth/token.py  (lines 42–67)
🔧 run_shell: pytest tests/test_auth.py
   → 3 passed, 1 failed (test_refresh_expired_token)
📝 edit_file: src/auth/token.py  (line 55)
🔧 run_shell: pytest tests/test_auth.py
   → 4 passed
🔧 run_shell: git diff --stat
   → 2 files changed, 18 insertions(+), 4 deletions(-)
⏳ Waiting for next action...
────────────────────────────────────────────────────────────────────────
[ Open in OpenHands ↗ ]    [ View Hatchet Run ↗ ]
```

Backend: `GET /api/ui/openhands/{task_id}/events` — polls the OpenHands API for conversation history on the active conversation, returns the action log. The openhands worker stores the `conversation_id` in Mem0 at task-start; this endpoint retrieves it and proxies the OpenHands event stream.

Frontend polls every 3s while status is live, stops when the worker writes its Mem0 completion summary.

---

### Panel 3: Dispatch Status Stream

Live view of any in-flight dispatch — not just OpenHands.

```
Task #1235 — coder "Fix null pointer"                        [2m 30s]
────────────────────────────────────────────────────────────────────────
Hatchet run: abc123   [ View in Hatchet ↗ ]
Mem0 summary: (pending — task still running)

Last heartbeat: 12s ago
────────────────────────────────────────────────────────────────────────
```

Backend: `GET /api/ui/status/{task_id}` — wraps the existing `GET /api/v1/status/{task_id}` (Mem0 poll) and adds the Hatchet run URL + elapsed time. Returns `{ task_id, done, mem0_summary, hatchet_url, elapsed_seconds, last_heartbeat }`.

Frontend auto-polls every 5s while `done=false`.

---

### Panel 4: Trigger Panel

Dispatch any agent without opening Vikunja or crafting a curl command.

```
Agent type: [ openhands ▾ ]
Repo:       [ amerenda/praetor ]
Title:      [ Fix null pointer in token refresh ]
Description:
  ┌─────────────────────────────────────────────┐
  │ The token refresh handler throws a NPE when │
  │ the refresh token is expired. See #88.      │
  └─────────────────────────────────────────────┘
[ Trigger → ]
```

Posts to `POST /api/v1/dispatch`. Response shows task_id and links to Hatchet run. New run appears in Run Dashboard within 10s.

---

### Panel 5: MCP Registry

View and manage registered MCPs. Health status comes from a direct k8s API call, tool count from LiteLLM — not via MCP.

```
Registered MCPs                                       [+ Register MCP]
┌──────────────────────┬──────────┬──────────┬────────────────────────┐
│ Name                 │ Status   │ Tools    │ Actions                │
├──────────────────────┼──────────┼──────────┼────────────────────────┤
│ mcp-searxng          │ ✓ Running│ 3        │ [History] [Remove]     │
│ github-mcp           │ ✓ Running│ 12       │ [History] [Remove]     │
│ kubernetes-readonly  │ ✓ Running│ 8        │ [History] [Remove]     │
│ kubernetes-rw        │ ⚠ Pending│ —        │ [History] [Remove]     │
└──────────────────────┴──────────┴──────────┴────────────────────────┘
```

Backend: `GET /api/ui/mcp` — aggregates:
- Registry entries from `GET /api/v1/mcp` (name, image, pr_url)
- Pod phase from k8s API (`GET /api/v1/namespaces/mcp-{name}/pods`) — returns Running / Pending / Failed
- Tool count from LiteLLM `GET /mcp/{name}/tools`

All three calls happen server-side in `ui_api.py`; the frontend gets a single merged response. Register form posts to `POST /api/v1/mcp/register`.

---

### Panel 6: Prompt Quick-Edit

Lists current production prompts from Langfuse.

```
Prompts
┌─────────────────────┬─────────┬────────────────┐
│ Name                │ Version │ Last modified  │
├─────────────────────┼─────────┼────────────────┤
│ coder-system        │ v4      │ 2026-06-15     │
│ research-system     │ v3      │ 2026-06-10     │
│ reviewer-system     │ v2      │ 2026-06-01     │
│ scaffold-system     │ v1      │ 2026-06-18     │
└─────────────────────┴─────────┴────────────────┘
[ Open in Langfuse ↗ ]
```

Backend: `GET /api/ui/prompts` proxies `GET /api/public/prompts` from the Langfuse API.

---

### Panel 7: Benchmark Runner

Run eval datasets from the UI without the CLI script (Phase 14 backend is reused).

```
Dataset:  [ research-eval ▾ ]    (5 items)
Model:    [ qwen3-35b ▾ ]
Prompt:   [ research-system:v3 ▾ ]
[ Run Benchmark → ]

Running... [3/5] ██████░░░░ 60%

Results:
  Mean score: 0.87
  Min score:  0.72
  [ View in Langfuse ↗ ]
```

Backend: `POST /api/ui/benchmark` — dispatches N `agent:benchmark` Hatchet events, streams progress via SSE.

---

### Panel 8: Scaffold Form

Form-based scaffold for new agents and MCP servers.

```
Type:  [ Agent ▾ ]
Name:  [ grafana-monitor ]
Description:
  ┌─────────────────────────────────────────┐
  │ Monitors Grafana alerts and creates     │
  │ Vikunja tasks when alerts fire.         │
  └─────────────────────────────────────────┘
[ Scaffold → ]
```

Backend: `POST /api/ui/scaffold` — calls `POST /api/v1/dispatch` with `type=scaffold`.

---

## New Backend Endpoints (`webhooks/ui_api.py`)

All endpoints are authenticated (same PRAETOR_API_KEY bearer auth as the rest of the API). These are thin aggregators — they call existing internal APIs and merge the results; no new storage.

```
GET  /api/ui/runs                    recent Hatchet runs merged with Langfuse trace IDs
GET  /api/ui/status/{task_id}        dispatch status + Hatchet URL + elapsed time
GET  /api/ui/openhands/{task_id}/events  OpenHands conversation event log for live tasks
GET  /api/ui/mcp                     MCP registry + pod health + tool counts
GET  /api/ui/prompts                 Langfuse prompt list (proxied)
POST /api/ui/benchmark               dispatch benchmark suite, SSE progress stream
POST /api/ui/scaffold                dispatch scaffold worker
```

## OpenHands conversation_id tracking

The openhands worker currently does not persist the OpenHands `conversation_id` anywhere the UI can retrieve it. For Panel 2 to work, `agents/openhands/worker.py` needs one change: write `conversation_id` to Mem0 at task-start (before the agent runs), under the key `"openhands-{task_id}-conversation"`. The `GET /api/ui/openhands/{task_id}/events` endpoint then reads it from Mem0 and proxies the OpenHands API.

## Deployment

New component in `praetor` repo: `praetor-ui`.

CI adds a build step for the `praetor-ui` image (nginx serving React static assets).

**k3s manifests:** Add `praetor-ui` Deployment + Service via app-factory. Existing `praetor.amer.dev` ingress routes:
- `/` → praetor-ui (nginx)
- `/api/` → webhook-adapter (FastAPI)
- `/webhooks/` → webhook-adapter (FastAPI, existing)

## Phase 28 Ready Conditions

1. `https://praetor.amer.dev` loads the control plane UI (requires auth)
2. Run Dashboard shows last 20 runs, auto-refreshing every 10s
3. Live OpenHands run: clicking [Watch] shows real-time action log polling every 3s
4. Dispatch status stream: `GET /api/ui/status/{task_id}` returns Hatchet URL + elapsed time
5. Trigger Panel: dispatch `type=openhands` → run appears in Run Dashboard within 10s
6. MCP Registry: lists all registered MCPs with actual pod phase and tool count (from k8s + LiteLLM directly, not via MCP)
7. Prompt Quick-Edit: lists all Langfuse prompts with correct versions
8. Benchmark Runner: 5-item eval suite completes and shows mean score inline
9. Scaffold Form: submit agent scaffold → draft PR opens on `amerenda/praetor` within 3 minutes
10. All existing webhook paths (`/webhooks/vikunja`, `/webhooks/github`, `/api/v1/dispatch`) still work
11. `praetor-ui` pod Running, multi-arch image built by CI
