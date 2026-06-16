# Praetor Platform — Overview

This document set breaks down the **Local LLM Agent Platform** plan into individual phase files for tracking and execution. Below is the complete architecture summary from `plan.md`.

## Intent

A multi-agent platform where different agents collaborate on tasks, share memory, and call local models — triggered automatically from external systems (Vikunja, GitHub, cron, eventually chat/voice). No custom agent loops, no custom harness code, no custom dispatcher.

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

Qdrant and Hatchet/Mem0 schemas both live in Mac Mini's existing PostgreSQL — separate databases, one server.

## Why These Choices

### Hatchet (not a custom dispatcher)

Hatchet is an open-source AI agent orchestration engine. It is exactly the dispatcher needed without writing one:

- **Triggers built-in:** cron schedules, webhooks (GitHub PR → worker), events API (any interface pushes an event, Hatchet dispatches the right worker), inter-service calls
- **Durable execution:** tasks survive worker crashes and restart from where they left off
- **Task tracking DB:** every run has status, logs, input, output, duration stored in PostgreSQL
- **Web UI:** full task history, retries, cancellation
- **Retry policies, timeouts, concurrency limits** — all config, no code
- **Hatchet Lite** = single Docker image + PostgreSQL only. No RabbitMQ, no Kafka.
- **Python SDK:** workers are decorated functions — minimal code

The "dispatcher" is now Hatchet. Writing one is off the table.

### PydanticAI (inside Hatchet workers)

- Native LiteLLM support
- 48% fewer tokens than CrewAI for equivalent tasks
- `Pydantic Graph` handles multi-agent state passing within a pipeline
- Stateless — fits naturally inside a Hatchet worker
- V1 stable API (Sep 2025), MIT licensed

### Mem0 + pgvector

- Mem0: stateless server on k3s, backed by PostgreSQL with pgvector extension
- The mem0 OSS REST server hardcodes pgvector as its vector store — no Qdrant env vars exist in the server
- All agents hit one Mem0 HTTP endpoint — concurrent writes handled by Mem0
- Qdrant (Phase 2) remains deployed on Mac Mini for future agents that need direct vector search
- No Neo4j (Graphiti ruled out — requires Neo4j v5.26+)

## Trigger Architecture

```
External events
──────────────────────────────────────────────────
  Vikunja (webhook)     GitHub PR webhook   Any future interface
         │                    │                (chat, voice, API)
         │                    │                      │
         └────────────────────┴──────────────────────┘
                              │
                           Hatchet
                  ┌─── Cron scheduler ────────────────┐
                  │    Webhook receiver                │
                  │    Events API                      │
                  │    Task DB (PostgreSQL)             │
                  │    Web UI (history, logs, retries) │
                  └───────────────────────────────────┘
                              │
               ┌───────────────┼───────────────┐
               │               │               │
         Research           Coder          PR Reviewer
         Worker             Worker          Worker
               │               │               │
               └───────────────┴───────────────┘
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
```

### How triggers work in Hatchet

**Vikunja webhook** — Vikunja POSTs to `https://praetor.amer.dev/webhooks/vikunja` on `task.updated` and `task.created` events (Vikunja has no `task.label.added` event — label changes arrive as `task.updated`). A FastAPI adapter validates the `X-Vikunja-Signature` HMAC-SHA256, inspects the task's current label list, and pushes the appropriate Hatchet event. If both `ai-research` and `ai-go` labels are present, it pushes `pipeline:research_code` directly. Idempotency key `vikunja-{task_id}-{label_id}` prevents duplicate runs.

**GitHub PR webhook** — Hatchet receives the GitHub webhook directly, dispatches to PR Reviewer or Pipeline workers based on event type and labels. Same pattern as Vikunja but using GitHub's HMAC validation (`X-Hub-Signature-256`).

**Cron** — registered in code at worker startup. E.g., every 30s → push `research:poll` event with `task_id`, every 5min → `test:ping`. Cron expression as env var, configurable via ConfigMap.

## Where Things Live

### Mac Mini (stateful core stack)

- **PostgreSQL** — existing installation; hosts Qdrant + Hatchet/Mem0 schemas in separate databases
- **Qdrant** — vector store at `10.100.20.18:6333`, Docker via Komodo GitOps
- **Komodo** — manages stateful services (Qdrant, PostgreSQL) from `komodo-dean-gitops`

### k3s cluster (ArgoCD via k3s-dean-gitops)

New Deployments:
- **LiteLLM** — inference gateway
- **Hatchet Lite** — dispatch engine, UI, cron, webhooks (points at Mac Mini PG)
- **Mem0 server** — memory API (points at Mac Mini PG via pgvector)
- **Agent workers** — one Deployment per agent type, pull tasks from Hatchet

## Phase Sequencing Rule

**Phases are strictly sequential. Phase N cannot begin until Phase N-1 is fully complete.**

Every phase file lists its pre-conditions. If a pre-condition is not met, stop and complete it before proceeding. Do not work on multiple phases in parallel. Do not skip a phase and return to it later — if a phase is blocked, resolve the blocker first.

The current phase is always the lowest-numbered phase that is not yet ✅ Complete in `status.md`.

## Phases Overview

| Phase | Name | Status | File |
|-------|------|--------|------|
| 0 | Deployment Foundation | ✅ Complete | [phase-00.md](./phase-00.md) |
| 1 | Inference | ✅ Complete | [phase-01.md](./phase-01.md) |
| 2 | Storage | ✅ Complete | [phase-02.md](./phase-02.md) |
| 3 | Dispatch | ✅ Complete | [phase-03.md](./phase-03.md) |
| 4 | Memory | ✅ Complete (verify in Phase 12) | [phase-04.md](./phase-04.md) |
| 5 | Research Agent | ✅ Complete | [phase-05.md](./phase-05.md) |
| 6 | Coder Agent | ✅ Complete | [phase-06.md](./phase-06.md) |
| 7 | PR Reviewer + QA | ✅ Complete (verify in Phase 12) | [phase-07.md](./phase-07.md) |
| 8 | Multi-Agent Pipeline | ✅ Complete | [phase-08.md](./phase-08.md) |
| 9 | Observability + Prompts | ✅ Complete | [phase-09.md](./phase-09.md) |
| 10 | MCP Gateway | ✅ Complete | [phase-10.md](./phase-10.md) |
| 11 | Scaffold Worker | ✅ Complete | [phase-11.md](./phase-11.md) |
| 12 | Platform Verification | ⬜ Next | [phase-12.md](./phase-12.md) |
| 13 | Agent Benchmarking & Eval | ⬜ Blocked on 12 | [phase-13.md](./phase-13.md) |
| 14 | Voice Dispatch | ⬜ Blocked on 13 | [phase-14.md](./phase-14.md) |
| 15 | Control Plane UI | ⬜ Blocked on 14 | [phase-15.md](./phase-15.md) |

## Non-Goals

This platform does **not** require:

- No custom dispatcher code
- No custom agent loop
- No custom task tracking DB schema
- No RabbitMQ / Kafka
- No Neo4j
- No n8n (Hatchet handles cron, webhooks, events natively)
- No Kubernetes-specific operator (Hatchet Lite is a single Docker image)
