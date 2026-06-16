# Phase 15 — Control Plane UI

**Goal:** A purpose-built React dashboard at `praetor.amer.dev` for platform operations — trigger agents, monitor runs, edit prompts, run benchmarks, and scaffold new components. Replaces tab-switching between Hatchet, Langfuse, and claw.amer.dev for routine platform tasks.

## Pre-conditions

- Phase 14 complete (voice dispatch working — all dispatch paths confirmed stable)
- `POST /api/v1/dispatch` and `GET /api/v1/status/{task_id}` live
- Benchmark runner from Phase 13 working (UI wraps existing backend)
- Scaffold worker from Phase 11 working (UI wraps existing `agent:scaffold` event)
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

Useful for: re-running failed tasks, testing prompt changes, dispatching ad-hoc research.

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

No inline editing — clicking "Open in Langfuse" opens `langfuse.amer.dev/prompts/<name>` in a new tab. The point is visibility, not duplication.

---

### Panel 4: Benchmark Runner

Run eval datasets from the UI without the CLI script (Phase 13 backend is reused).

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

Backend: `POST /api/ui/benchmark` — dispatches N `agent:benchmark` Hatchet events, streams progress via SSE. Frontend polls or uses SSE to update the progress bar.

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

Dispatched: agent:scaffold (task_id=1718400000)
Draft PR will appear on amerenda/praetor in ~2 minutes.
[ View Hatchet run ↗ ]
```

Backend: `POST /api/ui/scaffold` — calls `POST /api/v1/dispatch` with `type=scaffold`, formats the description into the structured spec the scaffold agent expects.

---

## Deployment

New component in `praetor` repo: `praetor-ui`.

**`ui/` directory:** React app, built by CI → static assets baked into an nginx image.

CI adds a build step:
```yaml
- name: Build UI
  run: |
    cd ui
    npm ci
    npm run build
- name: Build + push praetor-ui image
  uses: docker/build-push-action@v5
  with:
    context: ui
    file: ui/Dockerfile
    tags: amerenda/praetor-ui:${{ github.sha }}
    platforms: linux/amd64,linux/arm64
```

`ui/Dockerfile`:
```dockerfile
FROM nginx:alpine
COPY dist/ /usr/share/nginx/html/
COPY nginx.conf /etc/nginx/conf.d/default.conf
```

**k3s manifests:** Add `praetor-ui` Deployment + Service via app-factory. Existing `praetor.amer.dev` ingress routes:
- `/` → praetor-ui (nginx)
- `/api/` → webhook-adapter (FastAPI)
- `/webhooks/` → webhook-adapter (FastAPI, existing)

The `/webhooks/vikunja` and `/webhooks/github` paths are unchanged.

## Phase 15 Ready Conditions

1. `https://praetor.amer.dev` loads the control plane UI (requires auth)
2. Run Dashboard shows last 20 runs auto-refreshing every 10s
3. Trigger Panel: dispatch `type=research` → run appears in dashboard within 10s
4. Prompt Quick-Edit: lists all Langfuse prompts with correct versions
5. Benchmark Runner: 5-item eval suite completes and shows mean score inline
6. Scaffold Form: submit agent scaffold → draft PR opens on `amerenda/praetor` within 3 minutes
7. All existing webhook paths (`/webhooks/vikunja`, `/webhooks/github`, `/api/v1/dispatch`) still work
8. `praetor-ui` pod Running, multi-arch image built by CI
