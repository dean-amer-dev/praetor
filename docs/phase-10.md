# Phase 10 — MCP Gateway: Centralize Tools via LiteLLM

**Goal:** All MCP servers are registered in one place (LiteLLM). Agents connect to a single MCP endpoint instead of individual server URLs. Adding a new MCP server is a ConfigMap edit, not a code change in every agent.

## Pre-conditions

- Phase 9 complete (Langfuse live)
- Existing MCP servers deployed and reachable:
  - `https://mcp-searxng.amer.dev/mcp` — SearXNG web search
  - `https://infra-mcp.amer.dev` — app provisioning
  - `https://bws-mcp.amer.dev` — secrets access

## What LiteLLM MCP Gateway Does

LiteLLM's MCP Gateway provides a single aggregated endpoint (`https://litellm.amer.dev/mcp`) that proxies to all registered MCP servers. Clients connect once; LiteLLM handles routing to the right server based on the tool name.

Access is controlled per API key — an agent key can be scoped to only the MCP servers it needs.

This is **not** the same as LiteLLM's "Skills" feature (that's Claude Code CLI marketplace, unrelated to Praetor agents).

## What Gets Built

### Register MCP Servers in LiteLLM ConfigMap

Add `mcp_servers` block to the LiteLLM `configmap.yaml` in `k3s-dean-gitops/apps/litellm/`:

```yaml
mcp_servers:
  searxng:
    url: "https://mcp-searxng.amer.dev/mcp"
    transport: "streamable_http"

  infra-mcp:
    url: "https://infra-mcp.amer.dev/mcp"
    transport: "streamable_http"
    headers:
      Authorization: "Bearer ${INFRA_MCP_TOKEN}"

  bws-mcp:
    url: "https://bws-mcp.amer.dev/mcp"
    transport: "streamable_http"
    headers:
      Authorization: "Bearer ${BWS_MCP_TOKEN}"
```

Tokens for authenticated MCP servers are stored in BWS and injected into LiteLLM via ExternalSecret (same pattern as all other secrets).

### Update Agents to Use Gateway

Agents currently instantiate MCP clients pointing at individual server URLs. After this phase, they connect to the gateway:

```python
# Before (direct)
from fastmcp import FastMCPClient
searxng_client = FastMCPClient("https://mcp-searxng.amer.dev/mcp")

# After (gateway)
from fastmcp import FastMCPClient
mcp_client = FastMCPClient(
    os.environ["LITELLM_BASE_URL"].replace("/v1", "/mcp"),
    headers={"Authorization": f"Bearer {os.environ['LITELLM_API_KEY']}"},
)
```

PydanticAI agents can load tools directly from the MCP client:

```python
agent = Agent(
    model=model,
    system_prompt=get_system_prompt("coder-system"),
    tools=[*mcp_client.tools(), update_vikunja_task, run_shell, ...],
)
```

Tools from MCP servers (web_search, provision_app, etc.) become available to any agent without per-agent imports.

### Shared Tools That Move to MCP

Once the gateway is live, `update_vikunja_task` (currently duplicated in `agents/research/agent.py` and `agents/coder/agent.py`) can be extracted into a `vikunja-mcp` server in `dean-mcp`. That's the natural Phase 10 cleanup:

| Tool | Current location | Post-gateway location |
|------|-----------------|----------------------|
| `web_search` | `agents/research/agent.py` | `mcp-searxng` (already) |
| `update_vikunja_task` | duplicated in both agents | new `vikunja-mcp` in `dean-mcp` |
| `provision_app` | `infra-mcp` (already) | `infra-mcp` (already) |
| `get_secret` | `bws-mcp` (already) | `bws-mcp` (already) |

### Access Control Per Agent Key

In LiteLLM, each API key can be scoped to specific MCP servers:

```bash
# Create a coder-worker key with access to searxng and bws-mcp only
litellm key create --alias coder-worker --mcp-servers "searxng,bws-mcp"
```

This replaces the shared `LITELLM_API_KEY` with per-worker keys — a coder worker can't accidentally call `provision_app` on infra-mcp.

## Ready Conditions for Phase 10

1. `GET https://litellm.amer.dev/mcp/tools` returns a merged tool list from all registered MCP servers
2. Research agent calls `web_search` via the gateway — trace in Langfuse shows the MCP tool call
3. Adding a new MCP server (e.g., `vikunja-mcp`) is a ConfigMap edit + ArgoCD sync, no agent code change
4. `update_vikunja_task` extracted to `vikunja-mcp` in `dean-mcp`, both agents updated to use it via gateway
