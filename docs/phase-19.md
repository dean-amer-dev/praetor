# Phase 19 — Intelligent MCP Agent

**Goal:** When an agent or user identifies that a new capability is needed, the platform automatically determines whether an existing MCP covers it, and if not, writes and deploys one. Adding a new tool to the platform is a conversation, not a manual process.

## Pre-conditions

- Phase 18 complete (full app pipeline — repo creation and CI automation working)
- Phase 15 complete (MCP factory deployment API)
- Phase 11 complete (scaffold worker — MCP code generation)
- Research agent working (Phase 5)
- MCP factory `POST /api/v1/mcp/register` stable

## The Gap Today

Phase 15 gives you a deployment API: hand it a complete spec (image, port, secrets) and it opens a GitOps PR. What it doesn't do is think:

- Does an MCP already exist for this?
- Is an MCP even the right tool, or is a direct API call enough?
- If building new, what should it do exactly?

Those decisions currently require a human. This phase makes them automatic.

## Architecture

```
Trigger: user in OpenWebUI or agent during task execution
    │
    │  "I need to be able to query Grafana alerts"
    │  or: agent hits a tool gap mid-task
    │
    ▼
POST /api/v1/mcp/request   ← new endpoint (or OpenWebUI tool call)
    { capability: "query Grafana alerts from Grafana API" }
    │
    ├─► Research agent: search for existing MCP servers
    │     - GitHub search: "mcp server grafana"
    │     - Smithery / mcp.so registries
    │     - ModelContextProtocol GitHub org
    │     Returns: { found: bool, image: str | None, confidence: float, notes: str }
    │
    ├─► Decision:
    │     found + confidence > 0.8 → use existing image
    │     found + confidence < 0.8 → verify + test, then decide
    │     not found → scaffold new MCP
    │
    ├─► Path A: existing image found
    │     POST /api/v1/mcp/register { name, image, port, transport, env_secrets }
    │     Returns PR URL
    │
    └─► Path B: no existing image
          Dispatch scaffold worker: agent:scaffold
            { type: "mcp", name: <name>, description: <capability> }
          Scaffold worker:
            - Creates MCP server code in dean-mcp/<name>/
            - Opens PR on amerenda/dean-mcp
            - CI builds + pushes image (amerenda/<name>:latest)
          After PR merged + image built:
            POST /api/v1/mcp/register { name, image, ... }
          Returns: scaffold PR URL + (later) registry PR URL
```

## What Gets Built

### 20a — MCP Request Endpoint

```
POST /api/v1/mcp/request
{
  "capability": "natural language description of the tool capability needed",
  "preferred_name": "optional-name"    // optional
}
```

Returns:
```json
{
  "decision": "use_existing | scaffold_new",
  "image": "ghcr.io/org/mcp-name:latest",  // if use_existing
  "pr_url": "...",                          // registration or scaffold PR
  "research_summary": "...",               // what the research agent found
  "task_id": 12345                         // Hatchet run tracking the pipeline
}
```

### 20b — Research Agent MCP Search

Extend the research agent with an MCP discovery tool. Given a capability description, it searches:

1. **Smithery** (`smithery.ai`) — largest MCP index
2. **mcp.so** — community registry
3. **GitHub** — search `"mcp-server" <keyword>` for repos with Dockerfiles
4. **ModelContextProtocol org** — reference implementations
5. **Existing praetor MCPs** — check the registry via `GET /api/v1/mcp` first

Returns a confidence-ranked list of candidates with image names where available.

### 20c — Decision Logic

After research:

- `confidence >= 0.85` and image available on a public registry: use existing, call factory
- `confidence >= 0.85` but no prebuilt image: scaffold from source + build
- `confidence < 0.85`: ask user for confirmation before proceeding (via OpenWebUI or a Vikunja task comment)
- No results: proceed with scaffold_new

### 20d — Scaffold → Build → Register Pipeline

When scaffolding is needed, the flow is:

1. `agent:scaffold` event → scaffold worker creates `dean-mcp/<name>/server.py` + Dockerfile
2. Scaffold PR merged → CI builds `amerenda/<name>:latest`
3. Hatchet monitors for the CI completion (polls GitHub API for the build job)
4. On success: calls `POST /api/v1/mcp/register` automatically
5. Returns the final registry PR URL

This is a multi-step Hatchet workflow with a wait step between scaffold-merge and register.

### 20e — praetor_mcp Tool

Add `request_mcp(capability: str)` as a tool in the praetor-mcp MCP server. This is callable from OpenWebUI mid-conversation:

```
User: "Can you query my Grafana alerts?"
Model: "I don't have that tool yet. Let me find or build an MCP for it."
[model calls request_mcp("query Grafana alerts from Grafana HTTP API")]
Model: "Found mcp-grafana on Smithery. I'm registering it now — PR #... opened.
        Once merged, I'll have access to grafana_list_alerts, grafana_get_datasources, etc."
```

### 20f — Duplicate Prevention

Before any research, check `GET /api/v1/mcp` — if a registered MCP already covers the capability (by name or description match), return it immediately without opening a PR.

## Phase 19 Ready Conditions

1. `POST /api/v1/mcp/request { "capability": "search the web" }` → research agent finds mcp-searxng already registered, returns immediately with no new PR
2. `POST /api/v1/mcp/request { "capability": "query Grafana alerts" }` → research finds existing image, factory PR opened within 2 minutes
3. `POST /api/v1/mcp/request { "capability": "send a push notification to ntfy" }` → no existing MCP found → scaffold worker opens a PR on dean-mcp within 5 minutes
4. After scaffold PR merged + image built → register PR opened automatically (no human trigger)
5. `request_mcp` callable from OpenWebUI conversation mid-task
6. Confidence < 0.85 case: Hatchet run pauses and posts a clarification request before proceeding
