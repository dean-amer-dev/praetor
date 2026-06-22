# Phase 25 — Agent Factory

**Goal:** Go from "I want a new agent that does X" to a running, smoke-tested Hatchet worker with a Langfuse system prompt — in one API call or one conversation turn in OpenWebUI. Phase 11 (Scaffold Worker) opens the PR. Phase 25 closes the loop: merge, build, deploy, wire the event, create the prompt, verify it works.

---

## Pre-conditions

- Phase 24 complete
- Phase 11 complete (scaffold-worker live, `agent:scaffold` event working, Jinja templates in `praetor/templates/agent/`)
- Phase 16 complete (deploy PR pipeline proven — CI creates k3s-dean-gitops PR after image build)
- Langfuse API accessible for programmatic prompt creation

---

## What Gets Built

### New endpoint: `POST /api/v1/agent/create`

```python
class AgentCreateRequest(BaseModel):
    name: str           # kebab-case, e.g. "grafana-monitor"
    description: str    # natural language: what it does, what it has access to
    event: str          # Hatchet event name, e.g. "agent:grafana-monitor"
    tools: list[str] = []   # optional: hint which shared tools to wire (e.g. "search_memory", "web_search")
```

The endpoint orchestrates the full lifecycle — it does not return until the agent is running (or it times out with a status URL).

### Lifecycle

```
POST /api/v1/agent/create
    │
    ├─ 1. Dispatch agent:scaffold → scaffold-worker opens draft PR on amerenda/praetor
    │       PR includes: agents/{name}/agent.py, agents/{name}/worker.py, Dockerfile.{name}-worker
    │       PR also patches: webhook-adapter event routing to include the new event name
    │       PR also patches: CI detect-changes matrix to include the new component
    │
    ├─ 2. Merge the scaffold PR (auto-merge since it's a known-good skeleton)
    │
    ├─ 3. CI builds image → creates deploy PR on k3s-dean-gitops (existing pipeline)
    │
    ├─ 4. Auto-merge the deploy PR → ArgoCD rolls out the new worker pod
    │
    ├─ 5. Create Langfuse system prompt
    │       Name: {name}-system
    │       Initial content: generated from description + standard agent template
    │       (editable from UI immediately — the worker fetches it at startup)
    │
    ├─ 6. Smoke test
    │       Dispatch a test task to agent:{name} with a canary payload
    │       Poll Hatchet for up to 60s — verify the run completes (not errors)
    │
    └─ 7. Return status: { "agent": name, "event": event, "pod": ..., "langfuse_prompt": ..., "smoke_test": "passed" }
```

### Auto-merge policy

Steps 2 and 4 auto-merge because the scaffold output is deterministic (Jinja template) and the deploy PR contains only image tag changes — both are structurally safe to merge without human review. This matches the reasoning behind Phase 16's app pipeline.

If auto-merge is disabled or either PR fails CI, the endpoint returns a partial status with the PR URLs for manual completion.

### Scaffold PR content (what the scaffold-worker generates)

```
agents/{name}/
├── __init__.py
├── agent.py          ← PydanticAI Agent, tools wired from `tools` param
└── worker.py         ← Hatchet worker, event={event}, concurrency=1, retries=1

Dockerfile.{name}-worker   ← copies from Dockerfile.coder-worker pattern

# Patches to existing files:
webhook-adapter/router.py      ← adds event → worker mapping
.github/workflows/ci.yml       ← adds {name} to detect-changes component matrix
k3s/apps/praetor/{name}/       ← deployment + service manifests (new dir in scaffold PR)
```

### Langfuse prompt creation

Uses the Langfuse API to create a new prompt version:

```python
langfuse.create_prompt(
    name=f"{name}-system",
    prompt=render_template("system_prompt.jinja", name=name, description=description, tools=tools),
    labels=["production"],
)
```

The template produces a prompt in the same style as `coder-system` and `research-system` — it's immediately editable in the Langfuse UI without a redeploy.

### Smoke test

The smoke test dispatches:
```python
{"title": f"smoke-test-{name}", "description": "Verify the agent is reachable. Respond with OK.", "type": name}
```
to `agent:{name}` via Hatchet. A pass means the worker picked it up and returned a result within 60 seconds without erroring. The agent doesn't need to produce meaningful output — it just needs to not crash.

---

## OpenWebUI flow (via praetor-mcp)

```
User: "I need an agent that monitors Grafana alerts and creates Vikunja tasks"

Model: calls lm_praetor_create_agent({
    "name": "grafana-monitor",
    "description": "Monitors Grafana webhook alerts. On alert: searches mem0 for known remediation, creates a Vikunja task with severity + runbook link.",
    "event": "agent:grafana-monitor",
    "tools": ["search_memory", "add_memory"]
})

Model: "Agent grafana-monitor is live. Hatchet event: agent:grafana-monitor.
        System prompt at langfuse.amer.dev (grafana-monitor-system) — edit it to add
        the actual alert logic. Smoke test: passed."
```

---

## What Gets Added to `praetor-mcp`

New tool exposed at `/mcp`:

```
lm_praetor_create_agent(name, description, event, tools=[]) → status dict
```

This makes agent creation available from any OpenWebUI conversation, identical to how `lm_praetor_dispatch` triggers tasks and `lm_praetor_request_mcp` registers MCP servers.

---

## What Does NOT Get Built

- **Tool implementation** — the scaffold produces stubs. The model or human still fills in the actual tool logic. The factory wires the harness; it doesn't write the domain-specific code.
- **Eval dataset** — Phase 14 pattern (create eval dataset + baseline run) is not automated here. Phase 25 creates the agent and verifies it boots; ongoing quality tracking is a separate concern.
- **Removing agents** — no `DELETE /api/v1/agent` in this phase. Teardown is a manual k3s + GitHub operation.

---

## Ready Conditions

1. `POST /api/v1/agent/create` with a valid name + description returns within 5 minutes with a running pod
2. The new agent's Hatchet event (`agent:{name}`) is routable — dispatching to it reaches the correct worker
3. Langfuse shows `{name}-system` prompt, editable without a redeploy
4. Smoke test passes: the agent picks up the canary task and returns a result without crashing
5. `lm_praetor_create_agent` tool available in OpenWebUI via praetor-mcp — agent creation works from a chat message
6. A second call with the same `name` is idempotent: detects the existing agent, skips scaffold + deploy, returns current status
