# Praetor Platform — Overview

This document set breaks down the **Local LLM Agent Platform** plan into individual phase files for tracking and execution. Below is the complete architecture summary.

## Intent

A multi-agent platform where different agents collaborate on tasks, share memory, and call local models — triggered from a conversational interface (OpenWebUI), with GitHub, voice, and API as secondary entry points. No custom agent loops, no custom harness code, no custom dispatcher.

## Full Stack

| Layer | Tool | Stateful? | Where |
|-------|------|-----------|-------|
| Inference + queue | LiteLLM | No | k3s |
| Dispatch + triggers + tracking | Hatchet (Lite) | No (state in PG) | k3s |
| Agent harness + orchestration | PydanticAI + Pydantic Graph | No | inside Hatchet workers on k3s |
| Cross-session memory | Mem0 server | No (state in PG via pgvector) | k3s |
| Vector store | Qdrant | **Yes** | Mac Mini — core stack |
| Relational state | PostgreSQL | **Yes** | Mac Mini — core stack (existing) |

**Stateful services live on Mac Mini. Everything else is a stateless k3s Deployment.**

## Trigger Architecture

```
Primary interface
─────────────────────────────────────────────────────────────────
  OpenWebUI (bot.amer.dev)
  qwen3-35b-think
  praetor_mcp tool + infra_mcp tool wired in
         │
         │  User: "I want an app that does X"
         │  Model: creates plan, asks for approval
         │  User: approves
         │  Model: calls praetor_mcp tools → Hatchet events fire
         │
         ▼
      Hatchet
      ┌─── Events API ─────────────────────────────────────────┐
      │    Cron scheduler                                       │
      │    GitHub PR webhook                                    │
      │    Task DB (PostgreSQL)                                 │
      │    Web UI (history, logs, retries)                      │
      └────────────────────────────────────────────────────────┘
                            │
           ┌────────────────┼────────────────┐
           │                │                │
       Research           Coder          PR Reviewer
       Worker             Worker          Worker
           │                │                │
           └────────────────┴────────────────┘
                            │
                       PydanticAI
                  (harness, tool dispatch,
                   Pydantic Graph for multi-agent)
                            │
                   ┌─────────┴─────────┐
                   │                   │
               LiteLLM              Mem0
              (inference)          (memory)
                   │                   │
          Model containers        pgvector
                                (in Mac Mini PG)

Secondary interfaces
─────────────────────────────────────────────────────────────────
  GitHub PR events  →  reviewer + QA agents (automated, no human)
  Voice (HA)        →  POST /api/v1/dispatch (Phase 17)
  Vikunja task      →  webhook → Hatchet (optional, occasional use)
  Direct API        →  POST /api/v1/dispatch (curl, scripts, MCP)
```

## Primary User Flow

The intended interaction model is conversational:

```
1. User opens OpenWebUI (bot.amer.dev), using qwen3-35b-think
2. User describes what they want: "Build me an app that does X"
3. Model (with praetor_mcp + infra_mcp tools) creates a structured plan:
      - Does this need a new repo?
      - What tech stack?
      - Does it need MCP tools? (agent researches existing MCPs first)
      - Stateless k3s service or stateful?
      - What env secrets / integrations?
4. User reviews and approves (or modifies) the plan in chat
5. Model calls praetor_mcp tools to execute:
      - Create repo if needed (via coder GitHub App)
      - Add CI runners to the new repo
      - Dispatch coder agent with the approved plan
      - Coder opens a PR → reviewer fires → UAT deploys via CI → QA runs
      - Prod deploy PR waits for human approval
6. For MCPs specifically:
      - Research agent checks if an MCP already exists publicly
      - If yes: register it via the MCP factory (Phase 15)
      - If no: scaffold worker writes the code, CI builds the image, factory deploys it
```

**Vikunja tasks are optional** — a secondary input for tasks created manually or by scripts. Not the intended day-to-day path.

## CI/CD Pipeline (for stateless k3s apps)

Every new app follows this pipeline automatically once the repo exists:

```
push to main
    │
    ▼
CI: test → build → push image (sha-tag + latest)
    │
    ├──► UAT manifest update → commit directly to k3s-dean-gitops main
    │    ArgoCD auto-syncs → UAT pods rolling
    │
    └──► Prod deploy PR created (deploy/<app>-prod-*)
         Human reviews + approves → prod rolls
```

New repos are bootstrapped from `app-template` (Phase 19). Runners are provisioned via `infra-mcp.add_mac_mini_runner`.

## Why These Choices

### Hatchet (not a custom dispatcher)

- **Triggers built-in:** cron schedules, webhooks, events API
- **Durable execution:** tasks survive worker crashes
- **Task tracking DB:** every run has status, logs, input, output, duration
- **Web UI:** full task history, retries, cancellation
- **Hatchet Lite** = single Docker image + PostgreSQL only

### PydanticAI (inside Hatchet workers)

- Native LiteLLM support
- 48% fewer tokens than CrewAI for equivalent tasks
- `Pydantic Graph` handles multi-agent state passing within a pipeline
- Stateless — fits naturally inside a Hatchet worker

### Mem0 + pgvector

- Mem0: stateless server on k3s, backed by PostgreSQL with pgvector extension
- All agents hit one Mem0 HTTP endpoint — concurrent writes handled by Mem0
- Qdrant remains deployed on Mac Mini for future agents that need direct vector search

## Where Things Live

### Mac Mini (stateful core stack)

- **PostgreSQL** — existing; hosts Qdrant + Hatchet/Mem0 schemas in separate databases
- **Qdrant** — vector store at `10.100.20.18:6333`, Docker via Komodo GitOps
- **Komodo** — manages stateful services from `komodo-dean-gitops`

### k3s cluster (ArgoCD via k3s-dean-gitops)

- **LiteLLM** — inference gateway at `litellm.amer.dev`
- **Hatchet Lite** — dispatch engine, UI, cron, webhooks
- **Mem0 server** — memory API
- **Agent workers** — one Deployment per agent type
- **MCP servers** — under `apps/mcp/<name>/` in k3s-dean-gitops

### OpenWebUI (bot.amer.dev)

- Primary user interface
- `qwen3-35b-think` model — reasoning-enabled, tools wired in
- `praetor_mcp` tools: dispatch agents, check status, register MCPs
- `infra_mcp` tools: scaffold apps, provision services, add runners

## Phase Sequencing Rule

**Phases are strictly sequential. Phase N cannot begin until Phase N-1 is fully complete.**

Every phase file lists its pre-conditions. The current phase is always the lowest-numbered phase that is not yet ✅ Complete in `status.md`.

## Phases Overview

| Phase | Name | File |
|-------|------|------|
| 0 | Deployment Foundation | [phase-00.md](./phase-00.md) |
| 1 | Inference | [phase-01.md](./phase-01.md) |
| 2 | Storage | [phase-02.md](./phase-02.md) |
| 3 | Dispatch | [phase-03.md](./phase-03.md) |
| 4 | Memory | [phase-04.md](./phase-04.md) |
| 5 | Research Agent | [phase-05.md](./phase-05.md) |
| 6 | Coder Agent | [phase-06.md](./phase-06.md) |
| 7 | PR Reviewer + QA | [phase-07.md](./phase-07.md) |
| 8 | Multi-Agent Pipeline | [phase-08.md](./phase-08.md) |
| 9 | Observability + Prompts | [phase-09.md](./phase-09.md) |
| 10 | MCP Gateway | [phase-10.md](./phase-10.md) |
| 11 | Scaffold Worker | [phase-11.md](./phase-11.md) |
| 12 | Platform Verification | [phase-12.md](./phase-12.md) |
| 13 | OpenWebUI Integration | [phase-13.md](./phase-13.md) |
| 14 | Agent Benchmarking & Eval | [phase-14.md](./phase-14.md) |
| 15 | Self-Service MCP Factory | [phase-15.md](./phase-15.md) |
| 16 | Voice Dispatch | [phase-16.md](./phase-16.md) |
| 17 | Control Plane UI | [phase-17.md](./phase-17.md) |
| 18 | Full App Pipeline | [phase-18.md](./phase-18.md) |
| 19 | Intelligent MCP Agent | [phase-19.md](./phase-19.md) |
| 20 | Kubernetes MCP | [phase-20.md](./phase-20.md) |

## Non-Goals

- No custom dispatcher code
- No custom agent loop
- No custom task tracking DB schema
- No RabbitMQ / Kafka
- No Neo4j
- No n8n (Hatchet handles cron, webhooks, events natively)
- No Kubernetes-specific operator (Hatchet Lite is a single Docker image)
