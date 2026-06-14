# Phase 14 — Conversational Dispatch: claw.amer.dev → Praetor

**Goal:** A user at `claw.amer.dev` (OpenWebUI) can describe a task in natural language → the LLM calls a tool → a real Vikunja task is created + labeled → the existing webhook pipeline dispatches the right agent automatically. No manual Vikunja navigation required.

This phase is a pre-condition for Phase 11 (Scaffold Worker), which requires "a way to trigger Hatchet events from OpenWebUI."

## Pre-conditions

- Phases 5–9 complete and stable (research + coder agents healthy, Langfuse tracing live)
- claw.amer.dev running and connected to LiteLLM (Phase 1 + OpenWebUI deploy)
- Vikunja webhook registered on the target project (Phase 5 — already done)

## Architecture

```
User message at claw.amer.dev
        │
        ▼
   OpenWebUI (LLM turn)
   qwen3-35b via LiteLLM
        │  decides to call a tool
        ▼
  praetor_dispatch Tool
  (Python function, registered in OpenWebUI)
        │  POST /chat/dispatch
        ▼
  praetor webhook adapter
  (existing service, new endpoint)
        │  creates Vikunja task + applies label
        ▼
  Vikunja API
        │  Vikunja fires task.updated webhook
        ▼
  praetor adapter /webhooks/vikunja  ← existing flow from Phase 5
        │
        ▼
  Hatchet → agent:research / agent:code / pipeline:research_code
```

The chat tool does **not** push Hatchet events directly. It creates a real Vikunja task with the appropriate label. The existing Phase 5 webhook pipeline fires automatically and dispatches the agent — no duplicate dispatch logic.

## What Gets Built

### 1. Two New Endpoints on the Praetor Webhook Adapter

#### `POST /chat/dispatch`

Creates a Vikunja task + applies labels → webhook fires → agent dispatched.

```python
class ChatDispatchRequest(BaseModel):
    title: str
    description: str = ""
    task_type: Literal["research", "code", "pipeline"] = "research"
    project_id: int = 21  # Mycroft project — default home for AI tasks

class ChatDispatchResponse(BaseModel):
    task_id: int
    task_url: str
    dispatched: list[str]  # e.g. ["agent:research"] or ["pipeline:research_code"]
```

Implementation:
1. Validate `X-Praetor-Chat-Key` header against `PRAETOR_CHAT_API_KEY` env var
2. Create Vikunja task via `PUT /api/v1/projects/{project_id}/tasks`
3. Apply label(s) via `PUT /api/v1/tasks/{task_id}/labels`
   - `research` → label 14 (`ai-research`)
   - `code` → label 11 (`ai-go`)
   - `pipeline` → labels 14 + 11
4. Return `{task_id, task_url: "https://todo.amer.dev/.../{task_id}", dispatched}`

The Vikunja webhook fires within seconds of the label being applied (existing Phase 5 infra).

#### `GET /chat/status/{task_id}`

Returns current status of a dispatched task.

```python
class ChatStatusResponse(BaseModel):
    task_id: int
    task_url: str
    vikunja_done: bool
    vikunja_comment: str | None  # agent's output comment on the task
    mem0_summary: str | None     # first Mem0 result from task namespace
```

Implementation:
1. Validate `X-Praetor-Chat-Key`
2. `GET /api/v1/tasks/{task_id}` from Vikunja → `done`, `description`
3. `GET /api/v1/tasks/{task_id}/comments` from Vikunja → latest agent comment
4. `GET https://mem0.amer.dev/memories?agent_id=task-{task_id}` → first result
5. Return combined status

#### Secret: `PRAETOR_CHAT_API_KEY`

New secret in `praetor.toml` (generate=true). Added to webhook-adapter ExternalSecret and Deployment env. This is the only credential the OpenWebUI tool holds.

```toml
[[secrets]]
name        = "PRAETOR_CHAT_API_KEY"
bws_key     = "praetor-chat-api-key"
generate    = true
components  = ["webhook-adapter"]
```

### 2. OpenWebUI Tool: `praetor_dispatch`

A Python tool registered in OpenWebUI that exposes two functions to the LLM.

```python
"""
Tools for dispatching tasks to the Praetor AI agent platform.
"""
import httpx
from pydantic import BaseModel


class Valves(BaseModel):
    PRAETOR_BASE_URL: str = "https://praetor.amer.dev"
    PRAETOR_CHAT_KEY: str = ""  # set by admin — not shown to users


class Tools:
    def __init__(self):
        self.valves = Valves()

    def dispatch_task(
        self,
        title: str,
        description: str,
        task_type: str,
    ) -> str:
        """
        Dispatch a task to the Praetor agent platform.

        Use this when the user asks to:
        - Research a topic → task_type="research"
        - Implement code or open a PR → task_type="code" (description MUST include "repo: owner/name")
        - Both research then implement → task_type="pipeline"

        :param title: Short task title (what to do)
        :param description: Full task description. For code tasks, include "repo: owner/name".
        :param task_type: One of: research, code, pipeline
        :return: Confirmation with task ID and link
        """
        resp = httpx.post(
            f"{self.valves.PRAETOR_BASE_URL}/chat/dispatch",
            json={"title": title, "description": description, "task_type": task_type},
            headers={"X-Praetor-Chat-Key": self.valves.PRAETOR_CHAT_KEY},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return (
            f"Task #{data['task_id']} created and dispatched ({', '.join(data['dispatched'])}).\n"
            f"Track progress: {data['task_url']}"
        )

    def get_task_status(self, task_id: int) -> str:
        """
        Check the status of a previously dispatched Praetor task.

        :param task_id: The numeric task ID returned by dispatch_task
        :return: Current status including agent output if available
        """
        resp = httpx.get(
            f"{self.valves.PRAETOR_BASE_URL}/chat/status/{task_id}",
            headers={"X-Praetor-Chat-Key": self.valves.PRAETOR_CHAT_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data["vikunja_done"]:
            summary = data.get("vikunja_comment") or data.get("mem0_summary") or "Task completed."
            return f"Task #{task_id} is done.\n\nOutput: {summary}\n\nFull task: {data['task_url']}"
        else:
            return f"Task #{task_id} is still running. Full task: {data['task_url']}"
```

Tool is registered in OpenWebUI via the admin API:
```bash
curl -sf -X POST https://claw.amer.dev/api/v1/tools/ \
  -H "Authorization: Bearer $OPENWEBUI_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"id\": \"praetor_dispatch\", \"name\": \"Praetor Dispatch\", \"content\": $(cat tool.py | jq -Rs .), ...}"
```

A one-time registration script lives at `scripts/register_owui_tool.py`. After registration, the tool is available to all users (or can be restricted by model/workspace in OpenWebUI).

The `PRAETOR_CHAT_KEY` valve is set once via OpenWebUI admin UI (Settings → Tools → praetor_dispatch → Set Valve). It never leaves the OpenWebUI pod.

### 3. System Prompt Addition in Langfuse

The `claw-system` prompt (new Langfuse prompt, or added to `coder-system` / `research-system`) should include guidance for when to use the dispatch tool:

```
You have access to the Praetor agent platform via the dispatch_task tool.
Use it when the user asks you to:
- Research a topic in depth (task_type="research")
- Write code, open a PR, or implement something (task_type="code", include "repo: owner/name" in description)
- Both research AND implement (task_type="pipeline")

Do NOT use dispatch_task for:
- Quick questions you can answer directly
- Topics that don't require agent-level work
- Follow-up questions about an already-dispatched task (use get_task_status instead)
```

This prompt is registered in Langfuse as `openwebui-system` and assigned to the `claw.amer.dev` connection in OpenWebUI (System Prompt field under the model settings). Changing when the LLM dispatches is a Langfuse edit, not a code change.

### 4. Deployment Changes

**praetor webhook-adapter** — two new endpoints only. No new Deployment or Service; the existing webhook adapter pod handles them. `provision_app("praetor")` adds the new secret and redeploys.

**k3s-dean-gitops** — ExternalSecret for webhook-adapter updated to include `PRAETOR_CHAT_API_KEY`. Deployment env updated.

**No new services, no new ingresses, no new DNS entries.** The existing `praetor.amer.dev` ingress covers `/chat/*`.

## Conversation Examples

**Research:**
```
User: Can you research the current best practices for k3s HA setups?
LLM: [calls dispatch_task(title="Research k3s HA best practices", task_type="research")]
LLM: Task #147 created. The research agent is running — check back in a few minutes or ask me to get the status.
```

**Code:**
```
User: Add a /readyz endpoint to the praetor webhook adapter
LLM: [calls dispatch_task(title="Add /readyz endpoint to webhook adapter", description="repo: amerenda/praetor", task_type="code")]
LLM: Task #148 dispatched to the coder agent on amerenda/praetor. You'll get a PR link when it's done: https://todo.amer.dev/.../148
```

**Status check:**
```
User: What happened with task 147?
LLM: [calls get_task_status(task_id=147)]
LLM: Task #147 is done. Here's the summary: [agent output from Vikunja comment]
```

**Passthrough (no dispatch):**
```
User: What's the difference between Qdrant and pgvector?
LLM: [does not call dispatch_task — answers directly]
```

## Ready Conditions for Phase 14

1. `POST /chat/dispatch` with valid key + `task_type=research` → Vikunja task created, labeled `ai-research`, visible at `todo.amer.dev`
2. Within 60s of dispatch: praetor adapter `/webhooks/vikunja` fires for the new task, Hatchet UI shows `agent:research` run
3. `GET /chat/status/{task_id}` after research completes → `vikunja_done: true`, `mem0_summary` populated
4. `POST /chat/dispatch` with invalid key → 401
5. Chat session at `claw.amer.dev`: ask "research Tailscale subnet routing" → LLM calls `dispatch_task` → Vikunja task appears → agent runs
6. Chat session: "what's the status of task 150?" → LLM calls `get_task_status(150)` → returns agent output
7. General question ("explain BGP") → LLM answers directly, no `dispatch_task` call
8. Code dispatch: task description auto-includes `repo: amerenda/praetor` → `agent:code` dispatched, PR opened by `dean-coder[bot]`
9. `scripts/register_owui_tool.py` runs idempotently — re-running it updates the tool definition without creating duplicates
10. `PRAETOR_CHAT_KEY` valve set in OpenWebUI admin — non-admin users cannot read it
