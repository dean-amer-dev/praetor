# Phase 11 — Scaffold Worker: Create Agents and MCP Servers from OpenWebUI

**Goal:** Describe a new agent or MCP server in a chat message at `claw.amer.dev` (OpenWebUI) → scriptor opens a scaffolded PR on the right repo. Creating an agent or MCP server is a conversation, not a manual setup task.

## Pre-conditions

- Phases 6 and 10 complete (scriptor working, MCP gateway live)
- OpenWebUI at `claw.amer.dev` connected to LiteLLM
- A way to trigger Hatchet events from OpenWebUI (see Trigger section below)

## Trigger: OpenWebUI → Hatchet via Tool Call

OpenWebUI supports custom tools (Python functions exposed to the model). The scaffold trigger is a tool that POSTs to the Hatchet events API:

```python
# OpenWebUI Tool: trigger_scaffold
import httpx

async def trigger_scaffold(type: str, name: str, description: str) -> str:
    """
    Trigger scriptor to scaffold a new agent or MCP server.
    type: 'agent' or 'mcp'
    name: kebab-case name for the new component
    description: what it should do
    """
    resp = httpx.post(
        "https://hatchet.amer.dev/api/v1/events",
        json={
            "event_name": "agent:scaffold",
            "payload": {"type": type, "name": name, "description": description},
        },
        headers={"Authorization": f"Bearer {HATCHET_API_KEY}"},
    )
    return f"Scaffold triggered. Hatchet run ID: {resp.json().get('run_id')}"
```

Install this tool in OpenWebUI's Tool admin panel. The model can call it after the user describes what they want built.

Alternatively: use a dedicated `/scaffold` slash command in OpenWebUI that calls the same Hatchet endpoint directly, bypassing the model.

## What Gets Built

### Scaffold Worker — `praetor/agents/scaffold/`

Hatchet worker listening on `agent:scaffold`. Input:

```python
class ScaffoldInput(BaseModel):
    type: Literal["agent", "mcp"]
    name: str                    # e.g. "pr-reviewer" or "vikunja-mcp"
    description: str             # natural language description of what it should do
```

The scaffold worker runs scriptor (the coder agent) with a meta-prompt:

```
You are scaffolding a new {type} for the Praetor platform.
Name: {name}
Purpose: {description}

For type=agent:
  - Create agents/{name}/__init__.py, agent.py, worker.py following the exact pattern
    in agents/coder/ (PydanticAI agent + Hatchet v1.x worker)
  - Register it on a new Hatchet event: agent:{name}
  - Add it to requirements.txt if new deps are needed
  - Add Dockerfile.{name}-worker
  - Open a PR on amerenda/praetor

For type=mcp:
  - Create a new directory in dean-mcp/{name}/ with a FastMCP server
  - Follow the pattern in dean-mcp/infra-mcp/ (FastMCP, BWS secrets, k3s deployment)
  - Open a PR on amerenda/dean-mcp
  - Add a stub entry to the LiteLLM MCP gateway configmap

In both cases:
  - Branch: praetor-coder/scaffold-{name}
  - PR is a draft — human reviews before merging
  - PR description explains what was created and what still needs to be filled in
```

### Templates

To make scaffolded output consistent, store canonical templates in `praetor/templates/`:

```
praetor/templates/
├── agent/
│   ├── agent.py.jinja    ← PydanticAI agent skeleton
│   ├── worker.py.jinja   ← Hatchet worker skeleton
│   └── Dockerfile.jinja  ← worker Dockerfile
└── mcp/
    ├── server.py.jinja   ← FastMCP server skeleton
    └── Dockerfile.jinja  ← MCP server Dockerfile
```

Scriptor reads the template, fills in the name and description, and opens the PR. The resulting PR has working skeleton code — just needs the actual tool implementations added.

### Scaffold Worker Deployment

Add `scaffold-worker` component to `praetor` namespace:

```yaml
# deployment: same pattern as coder-worker
# image: reuse amerenda/praetor-coder:latest (same tools needed)
# emptyDir /scratch — git clones during scaffolding
# env: same as coder-worker (Hatchet, GitHub App, LiteLLM, Langfuse)
```

The scaffold worker is essentially the coder worker with a different Hatchet event name and a meta-level prompt. It can share the same image.

## Conversation Flow (Example)

```
User (OpenWebUI): "I need a new agent that monitors Grafana alerts and
                   creates Vikunja tasks when things go down"

Model: "I'll scaffold a grafana-monitor agent. It will listen on
        agent:grafana-monitor events and..."

[model calls trigger_scaffold(type="agent", name="grafana-monitor",
                              description="monitors Grafana alerts...")]

Model: "Scaffold triggered — scriptor is creating the PR now.
        Hatchet run ID: abc-123. Check https://hatchet.amer.dev for progress."

[~2 minutes later: draft PR open on amerenda/praetor]
```

## Ready Conditions for Phase 11

1. OpenWebUI tool `trigger_scaffold` installed and callable by the model
2. Describe a new agent in OpenWebUI → `agent:scaffold` Hatchet event fires within 10s
3. Draft PR opened on `amerenda/praetor` with working skeleton code (builds without errors)
4. Describe a new MCP server → draft PR opened on `amerenda/dean-mcp`
5. Scaffolded agent PR, when merged and image built, produces a running Hatchet worker (even if the tools are stubs)
