# Phase 13 — OpenWebUI Integration

**Goal:** Wire praetor-mcp, infra-mcp, and Mem0 into bot.amer.dev (OpenWebUI) so the model can dispatch Praetor agents, scaffold apps, and retain memory across conversations. Configure a full system prompt that describes the infrastructure, deployment patterns, and available tools.

## Pre-conditions

- Phase 12 complete (platform verified, mem0 confirmed healthy and functional at `https://mem0.amer.dev`)
- praetor-mcp deployed and healthy in k3s (`praetor-mcp` namespace)
- infra-mcp deployed and healthy in k3s (`infra-mcp` namespace)
- LiteLLM MCP gateway live with github-mcp and mcp-searxng already registered (Phase 10)

## Architecture

Two connection paths — do not conflate them:

```
praetor-mcp ─┐
             ├─► LiteLLM configmap (mcp_servers:) ─► mcp-bridge ─► OpenWebUI tool server
infra-mcp ───┘

Mem0 ──────────────────────────────────────────────► OpenWebUI MEMORY_PROVIDER (native)
```

- **praetor-mcp + infra-mcp** → LiteLLM MCP gateway via k3s-dean-gitops configmap → mcp-bridge auto-discovers and exposes as OpenAPI → OpenWebUI tool_server already pointed at mcp-bridge. Same pattern as github-mcp and mcp-searxng. No new infrastructure.
- **Mem0** → OpenWebUI native memory backend via `MEMORY_PROVIDER=mem0` env vars. This is NOT via the mcp-bridge. OpenWebUI has built-in Mem0 support — memories written in one conversation are retrieved in subsequent ones.

## What Gets Built

### 13a — Register praetor-mcp in LiteLLM configmap

Add to `apps/litellm/server/configmap.yaml` in k3s-dean-gitops, under `mcp_servers:`:

```yaml
mcp_servers:
  praetor:
    url: "http://praetor-mcp-server.praetor-mcp.svc.cluster.local:8000/mcp"
    transport: "http"
  infra:
    url: "http://infra-mcp-server.infra-mcp.svc.cluster.local:8000/mcp"
    transport: "http"
```

Open a k3s-dean-gitops PR. After merge and LiteLLM pod restart, the mcp-bridge picks up the new tools automatically (it re-fetches from LiteLLM on every `/mcp/openapi.json` request).

**Verify:** `curl https://mcp-bridge.amer.dev/mcp/openapi.json | jq '.paths | keys'` should show `praetor_mcp-dispatch_praetor_task`, `praetor_mcp-get_praetor_status`, `infra_mcp-scaffold_app`, `infra_mcp-provision_app`, `infra_mcp-open_deploy_pr`.

---

### 13b — Wire Mem0 to OpenWebUI

Add to the OpenWebUI pre_deploy in `resource-sync/stacks.toml` (komodo-dean-gitops):

```bash
printf 'MEM0_API_KEY=%s\n' "$(echo "$_BWSL" | jq -r '.[] | select(.key == "mem0-admin-api-key") | .value')" >> mac-mini-m4/openwebui/.env
printf 'MEM0_BASE_URL=https://mem0.amer.dev\n' >> mac-mini-m4/openwebui/.env
```

Add to `mac-mini-m4/openwebui/compose.yaml` environment block:

```yaml
ENABLE_MEMORY_TOOL: "true"
MEM0_BASE_URL: "https://mem0.amer.dev"
```

Then configure Mem0 as the memory provider in OpenWebUI admin: **Settings → Memory → Provider: Mem0**.

**Verify:** In an OpenWebUI conversation, tell the model "remember that murderbot has a RTX 4000 Blackwell GPU." Start a new conversation and ask "what GPU does murderbot have?" — it should recall from Mem0.

---

### 13c — Full system prompt for qwen3-35b-think

Set via **OpenWebUI admin → Models → qwen3-35b-think → System Prompt**:

```
You are Alex's personal AI assistant at bot.amer.dev.

## Hardware
- murderbot (local): RTX 4000 Blackwell 24 GB, K3s node, llama.cpp inference
- mac-mini-m4 (10.100.20.18): M4 16 GB, Komodo primary, Ollama, core services
  (DNS, PostgreSQL, MongoDB, Home Assistant, monitoring, Docker/OrbStack)
- archlinux (10.100.20.25): RX 9070 XT 16 GB, K3s node, Sunshine/Moonlight
- rpi5-0 (10.100.20.10), rpi5-1 (10.100.20.11), rpi4-0 (10.100.20.12): K3s nodes

## App Deployment
Two paths — all ingresses and TLS live in K3s regardless of path. Secrets always from Bitwarden (BWS), never hardcoded.
- Stateless → K3s (k3s-dean-gitops, ArgoCD): APIs, web services, workers.
  Flow: scaffold_app → provision_app → open_deploy_pr [→ open_ingress_pr if public URL needed]
- Stateful → Komodo (komodo-dean-gitops, mac-mini-m4): GPU workloads, persistent storage, Docker compose stacks.
  Flow: scaffold_app → provision_app → open_ingress_pr

## Praetor (AI Agent Platform — amerenda/praetor)
Praetor runs AI agent workflows on Hatchet. Three task types:
- research — web search + summarise; output written to Mem0
- code — reads a repo, implements changes, opens a PR (include `repo: owner/name` in description)
- pipeline — research then code

Dispatch: praetor_mcp-dispatch_praetor_task. Check: praetor_mcp-get_praetor_status.
Praetor also builds new MCPs via the MCP factory (Phase 15).
Docs and phase status: amerenda/praetor/docs/ (use github_mcp-get_repo_tree to explore)

## Tools
- github_mcp-*: bare name for amerenda repos ("praetor"), "owner/name" for others. On 404, search via mcp_searxng-web_search then retry.
- mcp_searxng-web_search / url_read: web search and page fetch
- praetor_mcp-dispatch_praetor_task / get_praetor_status: dispatch and monitor Praetor agent tasks
- infra_mcp-scaffold_app / provision_app / open_deploy_pr / open_ingress_pr: provision new apps

## Memory
Before answering questions about Alex's preferences, past decisions, ongoing projects,
or anything not described above — search memory first. Memory is updated by Praetor
research tasks and by Alex directly in conversation.
```

---

## Phase 13 Ready Conditions

1. `curl https://mcp-bridge.amer.dev/mcp/openapi.json | jq '.paths | keys'` shows `praetor_mcp-dispatch_praetor_task` and `infra_mcp-scaffold_app`
2. OpenWebUI model successfully dispatches a test research task via `praetor_mcp-dispatch_praetor_task`
3. OpenWebUI model successfully calls `infra_mcp-scaffold_app` with a test app description
4. Memory written in one OpenWebUI conversation is recalled in a new conversation (Mem0 round-trip)
5. System prompt visible and correct at bot.amer.dev admin → Models → qwen3-35b-think
