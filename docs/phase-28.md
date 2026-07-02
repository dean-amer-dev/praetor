# Phase 28 — Control Plane UI

## You are implementing Phase 28 of the Praetor platform.

**You are allowed to merge PRs for this session.**

---

## Context

You are working across `amerenda/praetor` (backend + React frontend) and `amerenda/k3s-dean-gitops` (k3s GitOps). The platform runs on a k3s cluster managed by ArgoCD. Secrets come exclusively from BWS. `praetor.amer.dev` is already an active ingress pointing to the webhook-adapter — you are adding a UI that sits behind the same domain.

### What already exists

- `amerenda/praetor` — FastAPI webhook-adapter serving `praetor.amer.dev/api/` and `/webhooks/`
- `GET /api/v1/mcp` — MCP registry
- `POST /api/v1/mcp/register`, `DELETE /api/v1/mcp/{name}` — MCP factory
- `GET /api/v1/status/{task_id}` — dispatch status (Mem0 + done flag)
- `POST /api/v1/dispatch` — agent dispatch
- `GET /api/v1/skills`, `GET /api/v1/agents` — skills API
- `POST /api/v1/benchmark/run` — benchmark runner (Phase 14)
- Hatchet at `https://hatchet.amer.dev`, Langfuse at `https://langfuse.amer.dev`
- Kubernetes MCP registered in LiteLLM — agents can query k8s, but the UI must NOT use MCP for status; it calls APIs directly

### What is missing

There is no control plane UI. Operators must tab between Hatchet, Langfuse, curl commands, and OWU to do routine platform work. Phase 28 builds `praetor.amer.dev/` as a unified operations dashboard.

### Architecture principle: no MCP in the UI

**MCP is for LLM-to-tool calls. The UI talks to REST APIs directly.** Pod health comes from the k8s API. Tool counts come from LiteLLM. Run history comes from Hatchet. OpenHands session state comes from the OpenHands API. All aggregation happens server-side in `webhooks/ui_api.py` — the React frontend gets single merged responses.

---

## Your constraints

**GitOps only.** All k3s manifest changes go through `k3s-dean-gitops` PRs. No `kubectl apply`. The new `praetor-ui` component deploys the same way every other praetor component does — CI builds the image, opens a deploy PR, you merge it, ArgoCD syncs.

**BWS is the single source of truth for all secrets.** The React app has no secrets. The `ui_api.py` backend uses secrets already available in the webhook-adapter pod (`LITELLM_API_KEY`, `PRAETOR_API_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `HATCHET_CLIENT_TOKEN`). No new BWS secrets needed unless a new dependency is introduced.

**No secrets in Git.** No API keys, tokens, or passwords in any committed file.

**Ansible-playbooks for infrastructure only.** This phase requires no host-level changes.

---

## What to build

### 1. `praetor/webhooks/ui_api.py` — new FastAPI router

Mount at `/api/ui/` in `webhooks/app.py`. All endpoints require the same `PRAETOR_API_KEY` bearer auth as the rest of the API.

```
GET  /api/ui/runs                        last 20 Hatchet workflow runs + Langfuse trace IDs
GET  /api/ui/status/{task_id}            dispatch status + Hatchet run URL + elapsed_seconds
GET  /api/ui/openhands/{task_id}/events  OpenHands conversation action log (live poll)
GET  /api/ui/mcp                         MCP registry + k8s pod phase + LiteLLM tool count
GET  /api/ui/prompts                     Langfuse prompt list (proxied)
POST /api/ui/benchmark                   dispatch benchmark suite, SSE progress stream
POST /api/ui/scaffold                    dispatch scaffold worker
```

**`GET /api/ui/mcp`** aggregates server-side:
- Registry entries from the existing `_load_registry()` function
- Pod phase: `GET {K8S_API}/api/v1/namespaces/mcp-{name}/pods` using the in-cluster service account token
- Tool count: `GET {LITELLM_BASE}/mcp/{name}/tools` (returns list of tool names)

Returns one merged list per MCP entry: `{ name, image, status (Running/Pending/Failed/Unknown), tool_count, pr_url, registered_at }`

**`GET /api/ui/openhands/{task_id}/events`** — reads `conversation_id` from Mem0 under key `openhands-{task_id}-conversation`, then proxies the OpenHands conversation history. Returns ordered list of `{ type, tool, args, result, timestamp }` entries. If no conversation_id is found, returns `{ events: [], status: "no_session" }`.

**`GET /api/ui/status/{task_id}`** — wraps the existing `_poll_mem0` call, adds `hatchet_url`, `elapsed_seconds`, `started_at` (from Mem0 write timestamp if available).

### 2. OpenHands worker change: store `conversation_id` at task-start

In `agents/openhands/worker.py`, immediately after creating the OpenHands conversation and before submitting the task, write to Mem0:

```python
await _add_memory(
    f"openhands-{task_id}-conversation",
    f"conversation_id: {conversation_id}",
    agent_id="openhands",
)
```

This is the only backend change needed to unlock the live view. It is a one-line addition.

### 3. `praetor/ui/` — React + Vite frontend

Tech: React, Vite, Tailwind (or plain CSS — keep it minimal). Build output: `dist/` served by nginx.

**8 panels:**

1. **Run Dashboard** — table of last 20 runs from `GET /api/ui/runs`. Columns: agent, task title, status (● Live / ✓ Done / ✗ Failed), duration, [Watch] or [View] link. Live runs have a [Watch] link that opens Panel 2. Auto-refreshes every 10s.

2. **OpenHands Live View** — polls `GET /api/ui/openhands/{task_id}/events` every 3s. Shows each action as it arrives (tool name, args summary, result snippet). Stops polling when `done=true` from the status endpoint. Shows elapsed time.

3. **Dispatch Status Stream** — polls `GET /api/ui/status/{task_id}` every 5s. Shows Hatchet run URL, elapsed time, Mem0 summary when done.

4. **Trigger Panel** — form that POSTs to `POST /api/v1/dispatch`. Fields: agent type dropdown (openhands, code, research, pipeline), repo (optional), title, description. On submit: shows returned task_id and link to Hatchet.

5. **MCP Registry** — table from `GET /api/ui/mcp`. Columns: name, status badge (Running/Pending/Failed), tool count, [History] and [Remove] buttons. [+ Register MCP] button opens a form that POSTs to `POST /api/v1/mcp/register`.

6. **Prompt Quick-Edit** — table from `GET /api/ui/prompts`. Columns: name, version, last modified. [Open in Langfuse ↗] button per row.

7. **Benchmark Runner** — form: dataset dropdown, model dropdown, prompt dropdown. Submit POSTs to `POST /api/ui/benchmark`. SSE stream shows progress `[N/total]` and final mean score. [View in Langfuse ↗] link on completion.

8. **Scaffold Form** — form: type dropdown (agent, mcp), name, description. Submit POSTs to `POST /api/ui/scaffold`. Shows returned task_id and Hatchet link.

### 4. `Dockerfile.praetor-ui`

Multi-stage build: Node build stage → nginx:alpine serve stage. Nginx config serves the Vite `dist/` output at `/`, proxying nothing (the React app calls `praetor.amer.dev/api/` directly).

### 5. CI + k3s manifests

Add `praetor-ui` to the `detect-changes` matrix in `build.yaml`. CI builds the image and opens a deploy PR on `k3s-dean-gitops` adding `apps/praetor/praetor-ui/deployment.yaml` + `service.yaml`.

Update the existing `praetor.amer.dev` ingress to route:
- `/ → praetor-ui` Service (nginx)
- `/api/ → webhook-adapter` Service (existing)
- `/webhooks/ → webhook-adapter` Service (existing)

---

## Deployment

1. PR on `amerenda/praetor` — all code changes (ui_api.py, openhands worker, Dockerfile.praetor-ui, CI matrix update) → CI builds two images (webhook-adapter + praetor-ui) → opens deploy PR on `k3s-dean-gitops` → merge → ArgoCD syncs
2. Verify ingress routing is correct before marking done

---

## Done when

1. `https://praetor.amer.dev` loads the control plane dashboard (auth via existing amer.dev SSO if wired, otherwise PRAETOR_API_KEY bearer in the React Valves)
2. Run Dashboard shows last 20 runs, auto-refreshes every 10s
3. Live OpenHands run: [Watch] shows real-time action log, polls every 3s
4. `GET /api/ui/status/{task_id}` returns `hatchet_url` and `elapsed_seconds` alongside the existing fields
5. MCP Registry shows actual pod phase (Running/Pending/etc.) — not the static "pending" from the registry — and tool count
6. Trigger Panel: dispatching `type=research` creates a Hatchet run visible in the Run Dashboard within 10s
7. `praetor-ui` pod in k3s namespace is `Running`
8. `webhook-adapter` pod in k3s namespace is `Running` (with openhands conversation_id change)
9. ArgoCD shows `praetor` application as `Healthy` and `Synced`
10. All existing webhook paths (`/webhooks/vikunja`, `/webhooks/github`, `/api/v1/dispatch`) continue to work — regression test a dispatch call after deploy
