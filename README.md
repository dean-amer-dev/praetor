# Praetor

Multi-agent platform that runs on a local k3s cluster. Agents are triggered from a conversation in OpenWebUI, dispatch through Hatchet, execute via PydanticAI workers, and share memory through Mem0. All inference goes through LiteLLM.

**Current phase:** Phase 22 — Coder Re-Dispatch Loop

**No LangChain.** The agent harness is PydanticAI. LangChain is not installed, not imported, not referenced anywhere in the codebase.

---

## Live Stack

### Inference

| Service | Location | Purpose |
|---------|----------|---------|
| **llama.cpp** | murderbot `:8088` | Inference backend — runs `qwen3-35b-think` (35B MoE) |
| **LiteLLM** | `litellm.amer.dev` | Inference gateway + MCP aggregator. Routes completions to llama.cpp. Exposes a single `/mcp` endpoint that proxies all registered MCP servers. Custom hook (`tool_strip_hook`) manages context budget and synthesis enforcement. |

### Dispatch

| Service | Location | Purpose |
|---------|----------|---------|
| **Hatchet Lite** | `hatchet.amer.dev` | Job scheduler. Receives events, runs durable workflows, tracks every run with status + logs. State lives in PostgreSQL on mac-mini. |
| **praetor-webhook-adapter** | `praetor.amer.dev` | FastAPI app. Receives GitHub PR webhooks and Vikunja task events, translates them into Hatchet events. Also hosts the REST API (`/api/v1/dispatch`, `/api/v1/mcp/register`, `/api/v1/app/create`, etc.) |

### Agent Workers (k3s, namespace `praetor`)

All workers are stateless Hatchet workers. Each listens for specific event types.

| Worker | Hatchet Event | What It Does |
|--------|--------------|--------------|
| **research-worker** | `agent:research` | Checks mem0 first, then web search + synthesis via LiteLLM MCP tools (`lm_web_search`, `lm_web_read_url`). Writes new findings to mem0 (`agent_id="research"`). Posts summary to Vikunja. |
| **coder-worker** | `agent:code` | Checks mem0 for repo-specific patterns first (`agent_id="coder-{owner}/{repo}"`), then clones repo, implements task, opens draft PR via praetor-coder GitHub App. Writes key decisions to mem0. |
| **reviewer-worker** | `agent:review` | Checks mem0 for known issue patterns (`agent_id="reviewer-{repo}"`), reviews PR diff, posts structured comment via amerenda-reviewer GitHub App. Writes new issue patterns to mem0 only if none were already found (check-before-write). |
| **qa-worker** | `agent:qa` | Runs Playwright browser tests against UAT deployments. |
| **pipeline-worker** | `agent:pipeline` | Orchestrates multi-agent DAGs (e.g., research → code → review). Uses PydanticAI `Graph`. |
| **scaffold-worker** | `agent:scaffold` | Generates MCP server code and opens PRs on dean-mcp. Used by the MCP factory. |
| **benchmark-worker** | `agent:benchmark` | Runs agent eval datasets, records scores to Langfuse. Normally scaled to 0 replicas. |
| **stub-worker** | — | Health check / development stub. |

### MCP Servers (tool providers)

LiteLLM aggregates these at `/mcp`. All tools get the `lm_` prefix when exposed to agents.

| MCP Server | Namespace | Tools Exposed |
|------------|-----------|--------------|
| **mcp-searxng** | `mcp-searxng` | `lm_web_search`, `lm_web_read_url` |
| **infra-mcp** | `infra-mcp` | `lm_infra_scaffold`, `lm_infra_provision`, `lm_infra_deploy_pr`, `lm_infra_add_runner`, `lm_infra_app_status` |
| **bws-mcp** | `bws-mcp` | `lm_bws_get_secret`, `lm_bws_list_secret_names` |
| **mem0-server** | `mem0` | `lm_mem0_add_memory`, `lm_mem0_search_memory` |
| **praetor-mcp** | `praetor-mcp` | `lm_praetor_dispatch`, `lm_praetor_status`, `lm_praetor_request_mcp` — callable from OpenWebUI mid-conversation |

### Observability

| Service | Location | Purpose |
|---------|----------|---------|
| **Langfuse** | `langfuse.amer.dev` | Traces every agent run (tool calls, tokens, latency). Hosts versioned system prompts — agents fetch the current prompt at startup. Stores eval scores. |

### Stateful Core (mac-mini-m4)

| Service | Port / Path | Purpose |
|---------|------------|---------|
| **PostgreSQL** | `10.100.20.18:5432` | Hosts Hatchet's task DB, Mem0's pgvector schema, Langfuse's trace DB |
| **Qdrant** | `10.100.20.18:6333` | Vector store — available for agents that need direct vector search |

---

## How It Fits Together

```
OpenWebUI (bot.amer.dev)
  qwen3-35b-think + praetor_mcp tools
         │
         │  User: "Build me an app that does X"
         │  Model: forms a plan, user approves
         │  Model: calls lm_praetor_dispatch → POST /api/v1/dispatch
         │
         ▼
praetor-webhook-adapter
  POST /api/v1/dispatch
         │
         ▼
Hatchet ── fires agent:research / agent:code / agent:pipeline events
         │
         ▼
PydanticAI workers
  ├── call LiteLLM /v1/chat/completions (qwen3-35b-think)
  ├── call LiteLLM /mcp tools (web search, infra, BWS, mem0)
  ├── read/write memory via mem0
  └── trace everything to Langfuse

Secondary triggers
  GitHub PR opened  →  pubhooks.amer.dev/praetor/webhooks/github
                     →  webhook-adapter  →  Hatchet agent:review
  Vikunja task label ai-go  →  webhook-adapter  →  agent:code
  Vikunja task label ai-research  →  webhook-adapter  →  agent:research
```

---

## GitHub Apps

| App | Purpose |
|-----|---------|
| **praetor-coder** | Coder worker uses this to clone repos, push branches, open draft PRs. Supports any org where the app is installed — installation is looked up dynamically per repo. |
| **amerenda-reviewer** | Reviewer worker uses this to post PR review comments. Installed on all `amerenda` org repos. |

---

## Code Layout

```
agents/
  coder/        — coder agent + worker
  research/     — research agent + worker
  reviewer/     — PR reviewer agent + worker
  qa/           — QA (Playwright) agent + worker
  benchmark/    — eval runner
  scaffold/     — MCP/app scaffold agent

common/
  github_app.py     — JWT → installation token (praetor-coder + reviewer apps)
  langfuse_tools.py — @observe() decorator, get_system_prompt()
  memory_tools.py   — add_memory(), search_memory() wrappers for mem0
  dispatch.py       — shared Hatchet dispatch function

pipelines/
  research_then_code.py  — PydanticAI Graph DAG: research → coder → reviewer

webhooks/
  app.py             — FastAPI routes: /dispatch, /mcp/register, /app/create, /status
  github_webhook.py  — GitHub PR event handler
  vikunja_webhook.py — Vikunja task label handler
  app_factory.py     — POST /api/v1/app/create pipeline
  mcp_factory.py     — POST /api/v1/mcp/register pipeline (generates YAML → inline LLM review loop → GitOps PR)

tests/
  unit/      — fast, no live services
  e2e/       — hits real Hatchet + LiteLLM endpoints
  stress/    — tool-calling stress scenarios (run manually)
```

---

## Phase Status

| Phase | Name | Status |
|-------|------|--------|
| 0–16 | Foundation through Full App Pipeline | ✅ Complete |
| 17 | Intelligent MCP Agent (`/api/v1/mcp/request`, research → register pipeline) | ✅ Complete |
| 18 | Kubernetes MCP (deploy `mcp-server-kubernetes` via Phase 17 pipeline) | ✅ Complete |
| 19 | Voice Dispatch | ⬜ Pending |
| 20 | Control Plane UI | ⬜ Pending |
| 21 | Mem0 Integration + Pre-PR Review Loop | ✅ Complete |
| 22 | Coder Re-Dispatch Loop (reviewer REQUEST_CHANGES → re-dispatch coder, cap 2) | ⬜ Next |
| 23 | Inline Arbitration (loop exhausted → focused LLM call, decision memo to mem0 + PR) | ⬜ Pending |

See `docs/status.md` for full notes. See `docs/phase-N.md` for each phase's design and ready conditions.
