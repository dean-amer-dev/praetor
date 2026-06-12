# Phase 12 — Custom Praetor Control Plane UI

**Goal:** A purpose-built interface at `praetor.amer.dev` for managing the agent platform — trigger workflows, monitor runs, browse prompt versions, initiate benchmarks, and scaffold new components. Replaces navigating between Hatchet UI, Langfuse, and OpenWebUI for platform operations.

## Pre-conditions

- Phases 9–11 complete (Langfuse, MCP gateway, scaffold worker)
- Clear picture of what the Phase 11 OpenWebUI experience is missing

## Design Principles

This is a control plane, not a chat interface. OpenWebUI (`claw.amer.dev`) stays as the conversational entry point. The Praetor UI (`praetor.amer.dev`) is for platform operations:

- **Triggering**: dispatch specific agent workflows without crafting a Vikunja task
- **Monitoring**: unified view of Hatchet runs + Langfuse traces, no tab-switching
- **Prompt management**: shortcut into Langfuse prompt editor for common prompts
- **Benchmarking**: select a dataset + model + prompt version → run → see scores
- **Scaffolding**: form-based agent/MCP creation (wraps Phase 11 scaffold worker)

## Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Frontend | React + Vite | Already in stack (ecdysis is React) |
| Backend | FastAPI | Already in praetor (webhooks app) — extend it |
| Auth | Same SSO as other amer.dev services | No new auth infra |

The backend is an extension of `praetor/webhooks/app.py` — add new API routes for triggering events, querying Hatchet, and proxying Langfuse. The frontend is a new `praetor/ui/` directory.

## Feature Areas

### 1. Run Dashboard

Pulls from Hatchet API (`GET /api/v1/workflows/runs`) + Langfuse traces API. Shows:
- Active runs (live-updating)
- Recent run history per agent type
- Click-through to full trace in embedded Langfuse iframe (or inline)

No re-implementing Hatchet's UI — just the summary view with direct links.

### 2. Trigger Panel

Dispatch any agent event without Vikunja:

```
Agent type: [ coder ▾ ]
Task ID:    [ 123     ]
Title:      [ Add /healthz to ecdysis ]
Repo:       [ amerenda/ecdysis ]
[ Trigger → ]
```

Posts to Hatchet events API. Useful for re-running failed tasks, testing new prompts, or running benchmark tasks.

### 3. Prompt Quick-Edit

Lists current production prompts from Langfuse. Click → opens Langfuse prompt editor in a modal (or new tab). Shows version history. "Promote to production" button calls Langfuse API directly.

### 4. Benchmark Runner

```
Dataset:      [ kubectl-diagnose ▾ ]
Model:        [ qwen3-35b ▾ ]
Prompt:       [ coder-system v3 ▾ ]
[ Run Benchmark → ]
```

Dispatches N Hatchet `agent:benchmark` runs (one per dataset item). Polls for completion. Shows scores inline when done. Links to full Langfuse eval results.

Requires a new lightweight `agent:benchmark` Hatchet event that runs an agent against a single eval item and returns a structured score.

### 5. Scaffold Form

Form version of the OpenWebUI scaffold conversation:

```
Type:  [ Agent ▾ ]
Name:  [ grafana-monitor ]
Description: [ textarea ]
[ Scaffold → ]
```

Dispatches `agent:scaffold` event, links to resulting Hatchet run.

## Deployment

New component in `praetor` repo:

- `ui/` — React app, built by CI → static assets served by nginx
- `praetor/api/` — new FastAPI router for UI-specific endpoints (trigger, benchmark, prompt list)

App-factory adds a second Deployment to the praetor stack: `praetor-ui` (nginx serving built assets) with a new ingress at `praetor.amer.dev/` (the existing webhooks ingress moves to `praetor.amer.dev/webhooks`).

## What This is Not

- Not a replacement for Hatchet's full UI (retries, logs, worker status) — keep using `hatchet.amer.dev` for those
- Not a replacement for Langfuse's full eval UI — keep using `langfuse.amer.dev` for deep analysis
- Not a chat interface — that's `claw.amer.dev`

## Ready Conditions for Phase 12

1. `https://praetor.amer.dev` loads the control plane UI (authenticated)
2. Run Dashboard shows last 10 Hatchet runs with status and link to trace
3. Trigger Panel: dispatch `agent:code` for a test task → run appears in dashboard within 5s
4. Prompt Quick-Edit: change `coder-system` prompt, promote to production → next coder run uses it
5. Benchmark Runner: run a 3-item eval dataset → scores appear inline
6. Scaffold Form: create a stub agent → draft PR opens on `amerenda/praetor`
