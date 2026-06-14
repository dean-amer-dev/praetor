# Phase 14 — Unified Dispatch: Any Interface → Praetor

**Goal:** Every interface (claw, opencode, Claude Code, OpenHands, Vikunja) dispatches agents through the same shared function. The Vikunja label path is ONE trigger, not the canonical one. Dispatching an agent from a chat message or an MCP tool call is a first-class operation that does not require creating a to-do item first.

## Pre-conditions

- Phases 5–9 complete and stable
- claw.amer.dev connected to LiteLLM (already done)
- `dean-mcp` repo exists with infra-mcp and bws-mcp deployed

## The Problem Being Solved

Today the dispatch logic lives inline in `webhooks/vikunja.py:72–87`. Every other interface that wants to dispatch an agent has to either (a) go through Vikunja, which means creating a task first, or (b) duplicate the `hatchet.event.push()` call. Neither is right.

The fix is a two-part refactor:

1. Extract the dispatch logic to `common/dispatch.py` — a shared function that all callers use, including the Vikunja webhook handler.
2. Expose that function as a first-class HTTP endpoint on the existing webhook adapter.

"Calls the same code as my todo" is not a metaphor — it is the literal requirement.

## Architecture

```
Vikunja webhook (label added)    ─┐
                                   │
POST /api/v1/dispatch (HTTP)      ─┤
                                   ├──► common/dispatch.py:dispatch_agent()
praetor-mcp tool (MCP)            ─┤        │
  └─ used by: opencode,            │        └──► hatchet.event.push()
              Claude Code,         │                    │
              OpenHands            │                    ▼
                                   │             agent:research
OpenWebUI Tool (claw.amer.dev)   ─┘             agent:code
                                                 pipeline:research_code
```

## What Gets Built

### 1. `common/dispatch.py` — Shared Dispatch Function

Extract the routing logic from `webhooks/vikunja.py` into a single shared function:

```python
from typing import Literal
from hatchet_sdk import Hatchet

AgentType = Literal["research", "code", "pipeline"]

EVENT_MAP: dict[AgentType, str] = {
    "research": "agent:research",
    "code":     "agent:code",
    "pipeline": "pipeline:research_code",
}


def dispatch_agent(
    hatchet: Hatchet,
    task_id: int,
    task_title: str,
    task_description: str,
    agent_type: AgentType,
) -> list[str]:
    """Push the appropriate Hatchet event(s). Returns list of event names pushed."""
    event = EVENT_MAP[agent_type]
    payload = {
        "task_id": task_id,
        "task_title": task_title,
        "task_description": task_description,
    }
    hatchet.event.push(event, payload, additional_metadata={"source_task_id": str(task_id)})
    return [event]
```

**`webhooks/vikunja.py` is updated** to call `dispatch_agent()` instead of inlining the `hatchet.event.push()` calls. No behaviour change — just deduplication. The label→event mapping stays in `common/dispatch.py`.

### 2. `POST /api/v1/dispatch` — Direct Dispatch Endpoint

New router at `webhooks/dispatch_api.py`, mounted on the existing FastAPI app.

```
POST /api/v1/dispatch
Authorization: Bearer <PRAETOR_API_KEY>

{
  "title":       "Research Tailscale subnet routing",
  "description": "Focus on exit nodes and ACL interaction.",
  "type":        "research"   # research | code | pipeline
}
```

Response:
```json
{
  "task_id":   1718400000,
  "event":     "agent:research",
  "hatchet_url": "https://hatchet.amer.dev"
}
```

Implementation:
1. Validate `Authorization: Bearer` header against `PRAETOR_API_KEY` env var (stored in BWS, generate=true)
2. Generate `task_id` as `int(time.time())` — no Vikunja dependency
3. Call `common/dispatch.py:dispatch_agent()` — same code as the webhook path
4. Return `{task_id, event, hatchet_url}`

No Vikunja task created. The agent is running. If the agent writes to Mem0 under `agent_id=task-{task_id}`, status can be retrieved later.

**Secret:** `PRAETOR_API_KEY` (generate=true) added to `praetor.toml` for the webhook-adapter component.

### 3. `GET /api/v1/status/{task_id}` — Status Endpoint

Polls the two places agents write output to:

```json
{
  "task_id": 1718400000,
  "done": true,
  "mem0_summary": "Tailscale subnet routing works by ...",
  "vikunja_task_id": null,
  "vikunja_task_url": null
}
```

- `mem0_summary`: calls `GET https://mem0.amer.dev/memories?agent_id=task-{task_id}` — returns first result, or null if not yet written
- `done`: true if mem0 has results (research/pipeline) or if a Vikunja comment exists (coder)
- If the task was also created in Vikunja (optional path, see below), `vikunja_task_id` is set

### 4. Optional: Vikunja Task Side-Effect

Callers that want a tracked work item can pass `create_vikunja_task: true`:

```json
{
  "title": "Research Tailscale subnet routing",
  "type":  "research",
  "create_vikunja_task": true
}
```

When set:
1. Dispatch API creates a Vikunja task (POST to Vikunja API with `VIKUNJA_TOKEN`)
2. The task ID from Vikunja becomes the `task_id` for Mem0 scoping
3. The agent writes output as a Vikunja comment on that task (existing agent behaviour)
4. Response includes `vikunja_task_url`

When not set (default):
- `task_id` is a synthetic timestamp — no Vikunja record
- Output lives only in Mem0 and Langfuse

This is the correct relationship: Vikunja is an optional output channel, not the trigger.

### 5. `praetor-mcp` — MCP Server in `dean-mcp`

New server alongside `infra-mcp/` and `bws-mcp/` in the `dean-mcp` repo. Wraps the dispatch API as MCP tools so any MCP-aware client can dispatch agents.

```python
# dean-mcp/praetor-mcp/server.py

@mcp.tool()
def dispatch_praetor_task(title: str, description: str, type: str) -> str:
    """
    Dispatch a Praetor agent task.
    type: research | code | pipeline
    For code/pipeline tasks, include 'repo: owner/name' in description.
    Returns task_id and confirmation.
    """
    resp = httpx.post(
        f"{PRAETOR_BASE}/api/v1/dispatch",
        json={"title": title, "description": description, "type": type},
        headers={"Authorization": f"Bearer {PRAETOR_API_KEY}"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return f"Dispatched {data['event']} — task_id={data['task_id']}"


@mcp.tool()
def get_praetor_status(task_id: int) -> str:
    """Check the status of a dispatched Praetor task."""
    resp = httpx.get(
        f"{PRAETOR_BASE}/api/v1/status/{task_id}",
        headers={"Authorization": f"Bearer {PRAETOR_API_KEY}"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data["done"]:
        return f"Done. Summary: {data['mem0_summary']}"
    return f"Still running. Check back shortly or view at https://hatchet.amer.dev"
```

Deployed as a k3s service via app-factory, exposed at `https://praetor-mcp.amer.dev/mcp`.

**Registration — same pattern as infra-mcp:**

| Client | Config location | How registered |
|--------|----------------|----------------|
| Claude Code | `~/.claude.json` | stdio or HTTP transport |
| opencode | `~/.config/opencode/opencode.json` | HTTP transport |
| OpenHands | Agent profile config | HTTP transport |

One MCP server registration. Every client that has it can dispatch agents.

### 6. OpenWebUI Tool for claw.amer.dev

A Python tool registered in OpenWebUI's tool library — the same dispatch API, no new protocol.

```python
class Tools:
    class Valves(BaseModel):
        PRAETOR_BASE_URL: str = "https://praetor.amer.dev"
        PRAETOR_API_KEY: str  # set by admin; not visible to users

    def dispatch_task(self, title: str, description: str, task_type: str) -> str:
        """
        Dispatch a Praetor agent task.
        Use for: research (task_type=research), coding (task_type=code, add 'repo: owner/name' to description), or both (task_type=pipeline).
        """
        resp = httpx.post(
            f"{self.valves.PRAETOR_BASE_URL}/api/v1/dispatch",
            json={"title": title, "description": description, "type": task_type},
            headers={"Authorization": f"Bearer {self.valves.PRAETOR_API_KEY}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return f"Task {data['task_id']} dispatched ({data['event']}). Ask me to check status in a few minutes."

    def get_task_status(self, task_id: int) -> str:
        """Check the status of a previously dispatched task."""
        resp = httpx.get(
            f"{self.valves.PRAETOR_BASE_URL}/api/v1/status/{task_id}",
            headers={"Authorization": f"Bearer {self.valves.PRAETOR_API_KEY}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data["done"]:
            return f"Done. {data['mem0_summary']}"
        return "Still running."
```

Tool is registered once via `scripts/register_owui_tool.py` (calls OpenWebUI admin API). The `PRAETOR_API_KEY` valve is set in the OpenWebUI admin panel — it is the same key as the MCP tool uses, stored in BWS under `praetor-api-key`.

## Interface Matrix

| Interface | Mechanism | Requires Vikunja? | Side-effect Vikunja task? |
|-----------|-----------|------------------|--------------------------|
| Vikunja label | Webhook → dispatch_agent() | Yes (it IS Vikunja) | Yes (already exists) |
| claw.amer.dev | OpenWebUI Tool → /api/v1/dispatch | No | Optional |
| opencode | praetor-mcp → /api/v1/dispatch | No | Optional |
| Claude Code | praetor-mcp → /api/v1/dispatch | No | Optional |
| OpenHands | praetor-mcp → /api/v1/dispatch | No | Optional |
| Direct HTTP | curl /api/v1/dispatch | No | Optional |

## Deployment Changes

**`praetor` repo:**
- `common/dispatch.py` — new shared dispatch function
- `webhooks/vikunja.py` — refactored to use `common/dispatch.py` (no behaviour change)
- `webhooks/dispatch_api.py` — new router (`/api/v1/dispatch`, `/api/v1/status/{task_id}`)
- `webhooks/app.py` — mount new router
- `praetor.toml` — add `PRAETOR_API_KEY` secret for webhook-adapter
- `scripts/register_owui_tool.py` — one-time tool registration

**`dean-mcp` repo:**
- `praetor-mcp/` — new FastMCP server

**`k3s-dean-gitops`:**
- New app entry for `praetor-mcp` (via `provision_app`)
- ExternalSecret for webhook-adapter updated with `PRAETOR_API_KEY`

**`~/.claude.json` / `~/.config/opencode/opencode.json`:**
- Register `praetor-mcp` HTTP transport

## Ready Conditions for Phase 14

1. `curl -sf -H "Authorization: Bearer $PRAETOR_API_KEY" -X POST https://praetor.amer.dev/api/v1/dispatch -d '{"title":"E2E test","type":"research"}' | jq .` → returns `{task_id, event: "agent:research"}`
2. Research agent completes → `GET /api/v1/status/{task_id}` returns `{done: true, mem0_summary: "..."}`
3. `POST /api/v1/dispatch` with bad key → 401
4. Vikunja webhook path unchanged: labeling a task still dispatches correctly (refactor is non-breaking)
5. `dispatch_praetor_task` MCP tool callable from Claude Code session — dispatches agent, run visible in Hatchet UI
6. `dispatch_praetor_task` callable from an opencode session — same result
7. Chat session at claw.amer.dev: "research subnet routing in Tailscale" → LLM calls `dispatch_task` → Hatchet run appears, no Vikunja task created unless requested
8. `create_vikunja_task: true` path: task appears in Vikunja with ai-research label, agent writes output as comment
9. All existing E2E and smoke tests still pass (webhook path untouched behaviourally)
