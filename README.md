# Praetor

Multi-agent platform that runs on a local k3s cluster. Agents are triggered from a conversation in OpenWebUI, dispatch through Hatchet, execute via PydanticAI workers, and share memory through Mem0. All inference goes through LiteLLM.

**Current phase:** Phase 21 — Coder Re-Dispatch Loop

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
| **research-worker** | `agent:research` | Checks mem0 first, then web search + synthesis via LiteLLM MCP tools (`lm_web_search`, `lm_web_read_url`). Writes new findings to mem0 (`agent_id="research"`). Posts summary back to the dispatcher. |
| **coder-worker** | `agent:code` | Checks mem0 for repo-specific patterns first (`agent_id="coder-{owner}/{repo}"`), then clones repo, implements task, opens draft PR via praetor-coder GitHub App. Supports create, edit, and comment on PRs via `github_api`. Writes key decisions to mem0. |
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
| **github-mcp** | `github-mcp` | `lm_github_*` — read-only GitHub operations |
| **kubernetes-mcp** | `mcp-kubernetes-mcp` | `lm_kubernetes_*` — cluster introspection |
| **praetor-mcp** | `praetor-mcp` | `lm_praetor_dispatch`, `lm_praetor_status`, `lm_praetor_request_mcp` — callable from OpenWebUI mid-conversation |

### Observability

| Service | Location | Purpose |
|---------|----------|---------|
| **Langfuse** | `langfuse.amer.dev` | Traces every agent run (tool calls, tokens, latency). Hosts versioned system prompts — agents fetch the current prompt at startup. Stores eval scores. |

### Stateful Core (mac-mini-m4)

| Service | Port / Path | Purpose |
|---------|------------|---------|
| **PostgreSQL** | `10.100.20.18:5432` | Hosts Hatchet's task DB, Mem0's pgvector schema, Langfuse's trace DB, Praetor skills registry |
| **Qdrant** | `10.100.20.18:6333` | Vector store — available for agents that need direct vector search |

---

## OpenWebUI — murderbot-v0

The user-facing model is **murderbot-v0** (`qwen3-35b-think-custom`), a custom OWU model. Its config is stored in OWU's SQLite database and survives container restarts. Tools and behavior are baked in — there is no per-request injection of tool calling.

| What | Detail |
|------|--------|
| **Base model** | `qwen3-35b-think` → routes to LiteLLM → llama.cpp on murderbot |
| **Tool calling** | `function_calling: native` — model returns structured `tool_calls` JSON, not XML |
| **Tools** | `server:mcp:lm` (all 16 LiteLLM MCP tools: `lm_web_search`, `lm_web_read_url`, `lm_github_*`, `lm_infra_*`) + `praetor_dispatch` Python tool (`dispatch_task`, `get_task_status`) |
| **Date filter** | Global OWU filter (`date_injector`) prepends `Today's date is YYYY-MM-DD (UTC).` to every system prompt — required for date-accurate searches since the model has no clock |

### Restoring after an OWU database wipe

If the OWU container is redeployed from scratch (volume deleted), `server:mcp:lm` survives (it's registered in the OWU admin UI and persists with the data volume). Everything else is restored by running:

```bash
OWUI_ADMIN_PASSWORD=$(bws secret get openwebui-dean-admin-password) \
  python scripts/register_owui_tool.py
```

The script is idempotent: registers `praetor_dispatch` Python tool, `date_injector` filter (sets active + global), and the `qwen3-35b-think-custom` model config.

### Smoke tests

```bash
SMOKE_TESTS=1 pytest tests/smoke/test_owui_tool_pipeline.py -v
```

Covers: model config correctness, date filter active+global, LiteLLM MCP reachable, date actually injected (live model call), praetor dispatch end-to-end, SearXNG reachable.

---

## How It Fits Together

All agent dispatch goes through OpenWebUI. The model calls `lm_praetor_dispatch` which hits
`POST /api/v1/dispatch` on the webhook-adapter. GitHub PR webhooks trigger the reviewer
automatically. Vikunja todo triggers are not yet active (outstanding).

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
  ├── call LiteLLM /mcp tools (web search, infra, BWS, mem0, github)
  ├── read/write memory via mem0
  └── trace everything to Langfuse

Automatic trigger
  GitHub PR opened  →  pubhooks.amer.dev/praetor/webhooks/github
                     →  webhook-adapter  →  Hatchet agent:review

Vikunja todo triggers: outstanding (not yet active)
```

---

## GitHub Apps

| App | Purpose |
|-----|---------|
| **praetor-coder** | Coder worker uses this to clone repos, push branches, open draft PRs. Supports any org where the app is installed — installation is looked up dynamically per repo. |
| **amerenda-reviewer** | Reviewer worker uses this to post PR review comments. Installed on all `amerenda` org repos. |

---

## Creating a New Agent

Agents follow a standard structure: a PydanticAI `Agent` in `agent.py`, a Hatchet worker in `worker.py`, and a `Dockerfile.{name}-worker`. Adding an agent currently involves these steps:

**1. Scaffold** — dispatch `agent:scaffold` (Phase 11 scaffold-worker) or `POST /api/v1/agent/create` (Phase 23, coming). The scaffold-worker opens a draft PR on this repo with:
- `agents/{name}/agent.py` — PydanticAI agent skeleton with shared tools wired (`search_memory`, `add_memory`, any domain tools)
- `agents/{name}/worker.py` — Hatchet worker listening on `agent:{name}`, concurrency=1, retries=1
- `Dockerfile.{name}-worker` — same base image as other workers

**2. Fill in tools** — edit the scaffolded `agent.py` to add the actual tool implementations. The skeleton wires the harness; the domain logic is still written by the coder agent or a human.

**3. Wire the Hatchet event** — add `agent:{name}` to the event routing in `webhooks/github_webhook.py`.

**4. CI + deploy** — merge the PR → `detect-changes` in CI builds `Dockerfile.{name}-worker` → publishes `amerenda/praetor-{name}:sha-*` → creates a deploy PR on `k3s-dean-gitops` → merge that PR → ArgoCD rolls out the pod.

**5. System prompt** — create `{name}-system` in Langfuse. The worker calls `get_system_prompt("{name}-system", fallback=...)` at startup; edit the prompt in the UI anytime without a redeploy.

**6. Assign skills** — via `POST /api/v1/agents/{name}/skills` (Phase 22). Skills are prompt-only additions that teach the agent domain-specific behaviors. Changes take effect on the next task run.

**7. Smoke test** — dispatch a canary task to `agent:{name}` via Hatchet and verify the worker picks it up.

Phase 23 (Agent Factory) automates steps 1–7 into a single `POST /api/v1/agent/create` call.

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
  app.py             — FastAPI routes: /dispatch, /mcp/register, /app/create, /status, /skills, /agents
  github_webhook.py  — GitHub PR event handler
  vikunja_webhook.py — Vikunja task label handler (code exists, not active as primary trigger)
  app_factory.py     — POST /api/v1/app/create pipeline
  mcp_factory.py     — POST /api/v1/mcp/register pipeline (generates YAML → inline LLM review loop → GitOps PR)
  skill_registry.py  — POST /api/v1/skills, /api/v1/agents/{name}/skills (Phase 22)

tests/
  unit/      — fast, no live services
  e2e/       — hits real Hatchet + LiteLLM endpoints
  stress/    — tool-calling stress scenarios (run manually)
```

---

## Phase Status

| Phase | Name | Status |
|-------|------|--------|
| 0–18 | Foundation through Kubernetes MCP | ✅ Complete |
| 19 | GitHub Write MCP (`create_pr`, `update_pr`, `comment_pr` as a centralized MCP server in dean-mcp) | ⬜ Pending |
| 20 | Mem0 Integration + Pre-PR Review Loop | ✅ Complete |
| 21 | Coder Re-Dispatch Loop (reviewer REQUEST_CHANGES → re-dispatch coder, cap 2; Mode B pr:/branch: parsing) | 🔄 In progress |
| 22 | Skills System (per-agent prompt-only skills via PostgreSQL + Langfuse; API CRUD + worker integration) | ⬜ Pending |
| 23 | Agent Factory (`POST /api/v1/agent/create` → scaffold → deploy → smoke test in one call) | ⬜ Pending |
| 24 | Inline Arbitration (loop exhausted → focused LLM call, decision memo to mem0 + PR) | ⬜ Pending |
| 25 | Voice Dispatch | ⬜ Pending |
| 26 | Control Plane UI (skills matrix, MCP registry panel, task log viewer — Praetor single pane of glass) | ⬜ Pending |

See `docs/status.md` for full notes and design details.
