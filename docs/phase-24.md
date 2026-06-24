# Phase 24 — Skills System

**Goal:** Give every agent a hot-swappable behavior layer. Skills are named, versioned prompt snippets stored in PostgreSQL and Langfuse. Workers inject them at task-start — no pod restart needed, changes take effect on the next task.

---

## What Was Built

### `common/db.py`

asyncpg connection pool, lazy-initialized on first use. Graceful no-op when `PRAETOR_DB_URL` is not set (workers remain functional without DB). Pool is reused within a process lifetime.

### `common/skills.py`

Two functions:

- `load_skill_assignments(agent_name)` — queries `praetor_agent_skills` for the agent's active skill names
- `assemble_prompt(agent_name, base)` — loads assignments, fetches each snippet from Langfuse via `get_system_prompt(f"skill-{name}", fallback="")`, appends non-empty snippets to `base`. Returns `base` unchanged if DB is unavailable or no skills are assigned.

### `webhooks/skills.py`

Full CRUD REST API:

```
GET    /api/v1/skills                        list all skills
POST   /api/v1/skills                        create {name, description, prompt}
GET    /api/v1/skills/{name}                 get skill
PUT    /api/v1/skills/{name}                 update prompt (new Langfuse version)
DELETE /api/v1/skills/{name}                 delete

GET    /api/v1/agents                        list agents + their active skills
GET    /api/v1/agents/{name}/skills          list skills for one agent
POST   /api/v1/agents/{name}/skills          assign {skill_name}
DELETE /api/v1/agents/{name}/skills/{skill}  remove assignment
```

`create_skill` and `update_skill` push the prompt text to Langfuse as `skill-{name}`. PostgreSQL stores the snapshot + metadata; Langfuse is the versioned canonical source.

### Worker integration

All workers (coder, research, reviewer, qa) and feature-pipeline call `assemble_prompt()` at task-start before building the agent. The global agent cache was removed — a fresh agent is built per task so the skill-augmented prompt is always current.

```python
base = get_system_prompt("coder-system", fallback=_FALLBACK)
full_prompt = await assemble_prompt("coder", base)
agent = build_agent(system_prompt=full_prompt)
```

### Database

Two tables on the existing mac-mini PostgreSQL (shared with Hatchet, Mem0, Langfuse):

```sql
CREATE TABLE praetor_skills (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    prompt      TEXT NOT NULL,
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

`PRAETOR_DB_URL` added to all 7 k3s worker deployments via ExternalSecret → Secret env var pattern.

### OWU tool

Four new methods on the `praetor_dispatch` OWU Python tool: `list_skills()`, `create_skill()`, `assign_skill()`, `remove_skill_assignment()`. The `murderbot-v0` system prompt was updated with a skills management section and example flow.

---

## Merged PRs

- `amerenda/praetor` PR #130 — skills system implementation
- `amerenda/k3s-dean-gitops` PRs #932, #933, #934 — PRAETOR_DB_URL env vars for all workers
- `amerenda/praetor` PR #132 — register_owui_tool.py updated with skills methods

Deployed sha: `ee8e4e8`

---

## E2E Test Result

Skill `test-file-header` created and assigned to the coder via API. Coder task dispatched — the output file `skills-test.py` opened with `# created by praetor-coder` exactly as instructed by the skill prompt. Skill and assignment cleaned up post-test.
