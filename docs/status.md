# Praetor Platform — Phase Status

| Phase | Name | Status | Notes |
|-------|------|--------|-------|
| 0 | Deployment Foundation | ✅ Complete | infra-mcp, bws-mcp, GitHub apps deployed |
| 1 | Inference | ✅ Complete | LiteLLM at litellm.amer.dev |
| 2 | Storage | ✅ Complete | Qdrant on Mac Mini via Komodo |
| 3 | Dispatch | ✅ Complete | Hatchet Lite on k3s, stub worker |
| 4 | Memory | ✅ Complete | mem0 arm64 image healthy; smoke + E2E verified (Phase 12a) |
| 5 | Research Agent | ✅ Complete | OWU dispatch → research worker (Vikunja label trigger: outstanding, code exists, not primary) |
| 6 | Coder Agent | ✅ Complete | OWU dispatch → coder worker, PRs via praetor-coder[bot] (Vikunja label trigger: outstanding) |
| 7 | PR Reviewer + QA | ✅ Complete | End-to-end verified via test PR #36; amerenda-reviewer[bot] posts reviews |
| 8 | Multi-Agent Pipeline | ✅ Complete | Dual-label tasks → pipeline:research_code DAG; pipeline-worker deployed |
| 9 | Observability + Prompts | ✅ Complete | Langfuse deployed; @observe() on all tools; eval scores wired; coder-system v4 prompt live; real Hatchet run traced with tool call spans (trace 7c8cb711) |
| 10 | MCP Gateway | ✅ Complete | mcp-searxng + LiteLLM gateway live; agents wired; 2 tools verified at /mcp endpoint |
| 11 | Scaffold Worker | ✅ Complete | agent:scaffold event; Jinja templates; draft PRs on praetor + dean-mcp from OpenWebUI |
| 12 | Platform Verification | ✅ Complete | All smoke tests pass (18/18); health check green; Phase 4+7 verified; OOM fix + MCP URL fix merged (k3s-dean-gitops #802) |
| 13 | OpenWebUI Integration | ✅ Complete | praetor-mcp exposes lm_praetor_dispatch, lm_praetor_status, lm_praetor_request_mcp to OWU; system prompt set on qwen3-35b-think |
| 14 | Agent Benchmarking & Eval | ✅ Complete | benchmark-worker deployed; baseline run `baseline-1781706219` complete (10/10); research-eval mean=1.000, reviewer-eval mean=0.846 recorded in eval-baselines.md |
| 15 | Self-Service MCP Factory | ✅ Complete | POST /api/v1/mcp/register → GitOps PR on k3s-dean-gitops; idempotent upsert + skip_manifests mode; 44 unit tests |
| 16 | Full App Pipeline | ✅ Complete | POST /api/v1/app/create; GitHub repo from template + infra-mcp provision + coder dispatch; 34 unit tests |
| 17 | Intelligent MCP Agent | ✅ Complete | POST /api/v1/mcp/request + request_mcp MCP tool; registry dedup + LLM research + use_existing/scaffold_new routing |
| 18 | Kubernetes MCP | ✅ Complete | mcp-server-kubernetes deployed via Phase 17 pipeline; github-mcp and kubernetes-mcp registered in LiteLLM configmap |
| 19 | GitHub Write MCP | ⬜ Pending | New MCP server in dean-mcp wrapping GitHub write ops: create_pr, update_pr, comment_pr, close_pr, push_branch. Removes github_api() from agent.py into a centralized MCP tool any agent can use. Deploy via mcp-factory. |
| 20 | Mem0 Integration + Pre-PR Review Loop | ✅ Complete | search_memory before every task; add_memory after key decisions; reviewer check-before-write pattern |
| 21 | Coder Re-Dispatch Loop | ✅ Complete | PR #118 merged. pr:/branch:/attempt: parsing in coder worker; Mode A (new PR) + Mode B (edit existing PR) prompts; reviewer re-dispatch on REQUEST_CHANGES (cap attempt<1); synchronize webhook on praetor-coder/ branches; 8 new unit tests. |
| 22 | Spec Layer + Planning Conversation | ⬜ Pending | OWU planning agent produces structured TOML spec before dispatch; Mem0-seeded pre-fill; user approves before agent runs; coder reads spec for unambiguous requirements. See phase-22.md. |
| 23 | Coder Context Management + Feature Decomposition | ⬜ Pending | Three-layer fix for coder OOM: (23a) truncate tool outputs at source; (23b) Mem0 progress checkpoints every 8 actions for crash recovery; (23c) multi-feature specs dispatch pipeline:feature_decompose — sequential inline coder runs, one feature per clean context, idempotent Mem0 resume. See phase-23.md. Requires Phase 22 spec layer. |
| 24 | Skills System | ⬜ Pending | Dynamic per-agent prompt-only skills. Storage: PostgreSQL (praetor_skills + praetor_agent_skills tables on existing mac-mini DB) — no pod restarts, instant updates. Workers query at task-start and assemble final prompt = base + active skill snippets. Skill text lives in Langfuse as skill-{name}. API: CRUD for skills + per-agent assignments. |
| 25 | Agent Factory | ⬜ Pending | POST /api/v1/agent/create → scaffold → wire Hatchet event → CI/deploy → smoke test in one call. Phase 11 scaffold-worker does the PR; this wraps the full pipeline. |
| 26 | Inline Arbitration | ⬜ Pending | Re-dispatch loop exhausted → focused LLM call → decision memo written to mem0 + PR comment |
| 27 | Voice Dispatch | ⬜ Pending | Voice input → OWU → lm_praetor_dispatch pipeline |
| 28 | Control Plane UI | ⬜ Pending | Praetor "single pane of glass": agent matrix (skills × agents toggle), MCP registry panel, task log viewer, skill prompt editor. Requires Phase 24 (skills API). NOT Ecdysis. |
| 29 | Model Factory | ⬜ Pending | Dispatch a model name → hardware fit check across all runners → download to best runner → LiteLLM GitOps PR → benchmark suite (tool calling, approvals, general capability, instruction following) → Langfuse report. See phase-29.md. |

---

## Platform-Wide Rules

### Mem0 — all agents, always

Every agent (coder, reviewer, research, QA, scaffold, and any future agents) MUST:
1. Call `search_memory` at the start of every task (before the agent runs)
2. Call `add_memory` at the end of every task (after the agent completes)
3. Write a summary to both the agent-scoped namespace (`"<agent>-<repo>"`) AND the shared `"planner-global"` namespace (Phase 22+)

No exceptions. This is not optional per-agent — it is a system requirement.
The `planner-global` namespace is what enables the OWU planning conversation to answer
"which blog?" without asking the user.

---

## Dispatch Model

All agent triggering goes through OpenWebUI → `lm_praetor_dispatch` → `POST /api/v1/dispatch` → Hatchet.

Secondary triggers (GitHub PR webhook → reviewer) are active. Vikunja label triggers (ai-go, ai-research) have code in `webhooks/vikunja_webhook.py` but are **not active as primary dispatch** — outstanding, will be wired in a later phase.

---

## OpenWebUI (bot.amer.dev)

- **URL:** https://bot.amer.dev
- **Email:** `amerenda@proton.me`
- **Password:** stored in BWS as `openwebui-dean-admin-password`

---

## Skills System Design (Phase 24)

### Why PostgreSQL, not ConfigMap

ConfigMap changes require pod restarts (even with Stakater Reloader) which disrupts running tasks. The existing PostgreSQL on mac-mini (shared with Hatchet, Mem0, Langfuse) gives instant updates with no pod interaction — workers query at task-start, not pod-startup.

### Storage

Two tables in the existing PostgreSQL on mac-mini:

```sql
CREATE TABLE praetor_skills (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    prompt      TEXT NOT NULL,          -- snapshot; canonical versioned text lives in Langfuse
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE praetor_agent_skills (
    agent_name  TEXT NOT NULL,
    skill_name  TEXT NOT NULL REFERENCES praetor_skills(name) ON DELETE CASCADE,
    assigned_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (agent_name, skill_name)
);
```

### Worker integration

At task-start (not pod-startup — hot-swappable without restart):

```python
async def _assemble_prompt(agent_name: str, base: str) -> str:
    assignments = await _load_skill_assignments(agent_name)  # queries DB
    snippets = [get_system_prompt(f"skill-{name}", fallback="") for name in assignments]
    return base + "\n\n" + "\n\n".join(s for s in snippets if s)
```

### API routes (webhook-adapter)

```
GET    /api/v1/skills                        list all skills
POST   /api/v1/skills                        create {name, description, prompt}
GET    /api/v1/skills/{name}                 get skill + current Langfuse version
PUT    /api/v1/skills/{name}                 update prompt (pushes new Langfuse version)
DELETE /api/v1/skills/{name}                 delete

GET    /api/v1/agents                        list agents with active skills
GET    /api/v1/agents/{name}/skills          list active skills for agent
POST   /api/v1/agents/{name}/skills          assign {skill_name}
DELETE /api/v1/agents/{name}/skills/{skill}  remove assignment
```

### What a skill looks like

```json
{
  "name":        "edit-pr",
  "description": "Teaches the agent how to check out an existing PR and push changes",
  "prompt":      "══ SKILL: Edit existing PR ══\nUse when description contains pr: <number>..."
}
```

Nothing executable. Named, versioned prompt text only. Tool grants are separate (MCP tools via LiteLLM).

### Agent skill matrix (example)

```
              edit-pr  comment-pr  web-research  post-review  request-changes
coder           ✓          ✓            ✓
research                              ✓
reviewer                                              ✓              ✓
```

---

## Completed Work Notes

### Phase 21 (merged PR #118)
- `agents/coder/worker.py`: parses `pr:`, `branch:`, `attempt:` from task description; builds Mode A (new PR) or Mode B (edit existing PR) prompt
- `agents/pr_reviewer/worker.py`: re-dispatches `agent:code` with `pr:/branch:/attempt:1` on REQUEST_CHANGES (capped at attempt < 1)
- `webhooks/github.py`: `synchronize` on `praetor-coder/` branches fires `github:pr_opened` with `attempt=1` so reviewer re-runs but coder doesn't loop
- `agents/pr_reviewer/agent.py`: dedup block removed — Hatchet GROUP_ROUND_ROBIN concurrency serializes per-PR events natively
- `coder-system` v10 in Langfuse — Mode A/B/C prompts live
- github-mcp and kubernetes-mcp registered in LiteLLM configmap

### OpenHands dispatch (june 2026)
- `agents/openhands/worker.py` complete — creates conversation, waits for AWAITING_USER_INPUT, sends task, polls until done, writes Mem0
- `common/dispatch.py` includes `type=openhands` → `agent:openhands` event
- `praetor-mcp` dispatch tool now accepts `openhands` as valid task_type (dean-mcp PR #26) — enables OWU dispatch
- Deploy: `praetor-openhands-worker` running in praetor namespace

### Conversational Dispatch (completed during Phase 3/13)
- `POST /api/v1/dispatch` and `GET /api/v1/status/{task_id}` live on praetor webhook-adapter
- `praetor-mcp` in dean-mcp exposes `lm_praetor_dispatch` to OpenWebUI — primary trigger
- Vikunja webhook trigger code exists but is not active — outstanding for future use
- PRs: praetor#35, dean-mcp#17, k3s-dean-gitops#800
