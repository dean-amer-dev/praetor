# Phase 26 — Control Plane UI

**Goal:** A purpose-built React dashboard at `praetor.amer.dev` for platform operations — trigger agents, monitor runs, edit prompts, run benchmarks, manage MCPs, and scaffold new components. Replaces tab-switching between Hatchet, Langfuse, and claw.amer.dev for routine platform tasks.

## Pre-conditions

- Phase 19 complete (voice dispatch working — all dispatch paths confirmed stable)
- `POST /api/v1/dispatch` and `GET /api/v1/status/{task_id}` live
- Benchmark runner from Phase 14 working (UI wraps existing backend)
- Scaffold worker from Phase 11 working (UI wraps existing `agent:scaffold` event)
- MCP factory from Phase 15 working (UI wraps `POST /api/v1/mcp/register`)
- Phase 12 health check passing (platform must be fully verified before adding UI complexity)

## Design Principles

This is a **control plane**, not a chat interface. OpenWebUI (`claw.amer.dev`) stays for conversation. Praetor UI (`praetor.amer.dev`) is for platform operations only.

Do not re-implement what Hatchet or Langfuse already do well. Link out to them for deep drill-downs.

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

Unified view of recent agent runs across Hatchet + Langfuse.

```
Last 20 runs
┌──────────┬──────────┬──────────┬──────────┬──────────┐
│ Agent    │ Task     │ Status   │ Duration │ Trace    │
├──────────┼──────────┼──────────┼──────────┼──────────┤
│ research │ #1234    │ ✓ Done   │ 3m 12s   │ [View]   │
│ coder    │ #1233    │ ✓ Done   │ 8m 45s   │ [View]   │
│ reviewer │ PR #88   │ ✗ Failed │ 1m 02s   │ [View]   │
└──────────┴──────────┴──────────┴──────────┴──────────┘
```

Backend: `GET /api/ui/runs` — pulls from Hatchet API, merges with Langfuse trace IDs. No re-implementation of Hatchet's full UI. "View" links go to `hatchet.amer.dev` or `langfuse.amer.dev`.

Auto-refreshes every 10s.

---

### Panel 2: Trigger Panel

Dispatch any agent without opening Vikunja or crafting a curl command.

```
Agent type: [ research ▾ ]
Title:      [ Research Tailscale exit node ACL interaction ]
[ Trigger → ]
```

Posts to `POST /api/v1/dispatch`. Response shows task_id and links to Hatchet run. New run appears in Run Dashboard within 10s.

---

### Panel 3: Prompt Quick-Edit

Lists current production prompts from Langfuse. Click to open the prompt editor.

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

Backend: `GET /api/ui/prompts` proxies `GET /api/public/prompts` from Langfuse API.

---

### Panel 4: Benchmark Runner

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

### Panel 5: Scaffold Form

Form-based version of the OpenWebUI scaffold conversation.

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

### Panel 6: MCP Registry

View and manage registered MCPs (Phase 15 backend).

```
Registered MCPs
┌──────────────────────┬──────────┬──────────┬───────────┐
│ Name                 │ Status   │ Tools    │ Actions   │
├──────────────────────┼──────────┼──────────┼───────────┤
│ mcp-searxng          │ ✓ Healthy│ 3        │ [Remove]  │
│ github-mcp           │ ✓ Healthy│ 12       │ [Remove]  │
│ kubernetes-readonly  │ ✓ Healthy│ 8        │ [Remove]  │
│ kubernetes-rw        │ ✓ Healthy│ 12       │ [Remove]  │
└──────────────────────┴──────────┴──────────┴───────────┘
[ + Register MCP ]
```

Backend: `GET /api/v1/mcp` (Phase 15 endpoint). Register form posts to `POST /api/v1/mcp/register`.

---

## Deployment

New component in `praetor` repo: `praetor-ui`.

CI adds a build step for the `praetor-ui` image (nginx serving React static assets).

**k3s manifests:** Add `praetor-ui` Deployment + Service via app-factory. Existing `praetor.amer.dev` ingress routes:
- `/` → praetor-ui (nginx)
- `/api/` → webhook-adapter (FastAPI)
- `/webhooks/` → webhook-adapter (FastAPI, existing)

## Phase 20 Ready Conditions

1. `https://praetor.amer.dev` loads the control plane UI (requires auth)
2. Run Dashboard shows last 20 runs auto-refreshing every 10s
3. Trigger Panel: dispatch `type=research` → run appears in dashboard within 10s
4. Prompt Quick-Edit: lists all Langfuse prompts with correct versions
5. Benchmark Runner: 5-item eval suite completes and shows mean score inline
6. Scaffold Form: submit agent scaffold → draft PR opens on `amerenda/praetor` within 3 minutes
7. MCP Registry: lists all registered MCPs with tool counts and health status
8. All existing webhook paths (`/webhooks/vikunja`, `/webhooks/github`, `/api/v1/dispatch`) still work
9. `praetor-ui` pod Running, multi-arch image built by CI
