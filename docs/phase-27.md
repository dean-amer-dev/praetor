# Phase 27 — Spec Layer + Planning Conversation

**Goal:** Give every task a structured, human-approved spec before any agent runs.
Replace the current free-form string dispatch model with a typed spec that encodes
repo, stack, features, infra, and task type — produced by a conversational planning
session in OWU, seeded by Mem0 lookups, and approved by the user before dispatch.

**Prerequisite phases:** 21 (re-dispatch loop), 20 (Mem0 integration)  
**Blocks phases:** 28+ (every downstream agent improvement assumes structured input)

---

## Problem Being Solved

The current dispatch chain:

```
User: "build me a stateless frontend blog homepage"
OWU:  lm_praetor_dispatch(title="Blog", description="build me a stateless frontend blog", type="code")
Coder receives: {task_id: 5, task_title: "Blog", task_description: "build me a stateless frontend blog"}
```

The coder must infer from that one sentence: which repo, what framework, what k3s namespace,
what hostname, what port, what the features actually are, which repos to touch (app repo AND
k3s-dean-gitops), and what "blog" means as implemented code. It guesses. It uses tokens on
requirements interpretation instead of implementation. Two identical dispatches produce
inconsistent results.

The spec layer answers all of those questions **before** the coder runs.

---

## What Already Exists (do not duplicate)

Phase 16 built `POST /api/v1/app/create` which already does:
1. Creates GitHub repo from `amerenda/app-template`
2. Calls `infra-mcp /app/create` to provision k3s manifests + CI runner
3. Dispatches `agent:code` with a structured `_build_coder_description(plan)` prompt

`AppPlan` in `webhooks/app_factory.py` is already a typed spec for new apps:
```python
class AppPlan(BaseModel):
    name: str
    description: str
    domain: str | None = None
    port: int = 8000
    has_database: bool = False
    env_secrets: dict[str, str] = {}
    stateless: bool = True
```

The spec layer extends this pattern to cover all task types, not just new apps,
and adds the conversational planning step and Mem0 seeding that are currently missing.

---

## Spec Format

TOML in a fenced block, embedded in the task description string. No API schema changes
required — the existing description field carries the spec. The coder worker detects
a `[task]` TOML block and parses it; if absent, falls back to current `repo:` parsing.

### Task type: `new_app`

```toml
[task]
type        = "new_app"
title       = "Blog homepage"
description = "Stateless React frontend — post listing, individual post pages, markdown"

[repos]
primary = "amerenda/blog"
gitops  = "amerenda/k3s-dean-gitops"

[infra]
type          = "stateless"
k3s_namespace = "apps"
hostname      = "blog.amer.dev"
port          = 3000

[app]
runtime   = "node"
framework = "react-vite"
features  = [
    "Post listing page with title, date, excerpt",
    "Individual post page with markdown rendering",
    "Dark mode toggle (persisted in localStorage)",
    "Responsive layout, mobile-first",
]

[dispatch]
agents           = ["app_factory", "coder"]
reviewer_enabled = true
redispatch_cap   = 3
request_limit    = 80

[context]
prior_memory = []
```

**`agents` field for `new_app`:**
- `"app_factory"` — routes to the existing `POST /api/v1/app/create` handler
- `"coder"` — dispatched automatically by `app_factory` after provision completes (already implemented in `_provision_and_dispatch`)

The spec replaces the current `AppPlan` JSON body. The new `POST /api/v1/spec/execute`
endpoint parses the spec, builds an `AppPlan` from the `[infra]` and `[app]` sections,
and calls the existing `app_factory` logic. Nothing in `app_factory.py` changes.

### Task type: `modify_app`

```toml
[task]
type        = "modify_app"
title       = "Blog rewrite — add react-markdown"
description = "Replace current markdown handling with react-markdown v9"

[repos]
primary = "amerenda/blog"

[app]
changes = [
    "Remove manual string split in PostPage.tsx (line ~42)",
    "Install react-markdown@9 and remark-gfm",
    "Wrap all post content in <ReactMarkdown> component",
    "Update PostPage.tsx and BlogIndex.tsx",
]

[dispatch]
agents           = ["coder"]
reviewer_enabled = true
redispatch_cap   = 2
request_limit    = 60

[context]
prior_memory = [
    "blog = amerenda/blog, React 18 + Vite 5, deployed 2026-04-10",
    "markdown currently parsed with string split in PostPage.tsx (brittle)",
]
```

### Task type: `fix_pr`

```toml
[task]
type  = "fix_pr"
title = "Fix async loop — praetor PR #82"

[repos]
primary = "amerenda/praetor"

[pr]
number   = 82
branch   = "praetor-coder/task-77"
feedback = """
The dispatch loop in worker.py:88 is synchronous. Blocks the event loop
during the GitHub API call. Convert to asyncio with httpx.AsyncClient.
"""

[dispatch]
agents           = ["coder"]
reviewer_enabled = true
redispatch_cap   = 1
request_limit    = 40

[context]
prior_memory = [
    "praetor uses pydantic-ai; all tools must be async or use run_in_executor",
]
```

---

## The Planning Conversation

The OWU planning model's job is to have a short conversation and produce a spec.
It is NOT the coder — it never writes code. Its system prompt lives in Langfuse
as `planner-system`.

### What the planner does on every message:

1. **Mem0 lookup first.** Before asking any clarifying questions, call
   `lm_praetor_memory_search` with the user's message. If results come back,
   pre-fill known fields silently.

2. **Ask only what's missing.** If the user said "build me a stateless frontend
   blog at blog.amer.dev with React Vite, post listing, markdown, dark mode,"
   no questions are needed — every field is answerable. If the user said just
   "build me a blog," ask: name/hostname, framework (offer React+Vite as default),
   what the homepage should show.

3. **Produce the spec and show it.** When all required fields are known, produce
   the TOML spec in a fenced block and present it:
   ```
   Here's the plan — does this look right?

   ``` toml
   [task]
   type = "new_app"
   ...
   ``` 

   This will: scaffold amerenda/blog, provision k3s manifests at blog.amer.dev,
   then the coder writes the React + Vite frontend with all four features and
   opens a draft PR.
   ```

4. **Wait for explicit approval.** Never call `lm_praetor_execute_spec` without
   the user saying "yes," "looks good," "go," or similar. Allow edits:
   "change the framework to Vue" → update spec → show again.

5. **Dispatch.** Call `lm_praetor_execute_spec(spec=<toml>)`. Return the Hatchet
   task URL.

### Required fields per task type:

| Field | `new_app` | `modify_app` | `fix_pr` |
|-------|-----------|--------------|----------|
| repo name | yes | yes (from Mem0 if known) | yes (from Mem0 if known) |
| hostname | yes | no | no |
| framework | yes (default: react-vite or python-fastapi) | no | no |
| features/changes | yes | yes | no (comes from PR feedback) |
| pr_number + branch | no | no | yes |

---

## Mem0 Integration in Planning

The planner needs Mem0 access. This is currently only available inside agent workers.
A new MCP tool exposes it to OWU:

### New tool: `lm_praetor_memory_search`

Location: `dean-mcp/praetor-mcp/` alongside the existing `lm_praetor_dispatch` tool.

```python
def lm_praetor_memory_search(query: str) -> str:
    """
    Search Praetor's shared memory for prior context about repos, apps, and decisions.
    Call this before asking the user clarifying questions — the answer may already be known.
    Returns relevant memory entries, or 'No prior memory found.' if none.
    """
    import httpx, os
    resp = httpx.post(
        f"{os.environ['MEM0_BASE_URL']}/search",
        json={"query": query, "agent_id": "planner-global"},
        headers={"x-api-key": os.environ["MEM0_API_KEY"]},
        timeout=10,
    )
    results = resp.json().get("results", [])
    if not results:
        return "No prior memory found."
    return "\n".join(r["memory"] for r in results[:5])
```

The `agent_id = "planner-global"` is a new shared namespace. All agents that complete
a task write a short summary to both their own agent-scoped namespace AND to
`"planner-global"`, so the planner can find it.

**Example of what Mem0 returns for "rewrite the blog":**
```
blog = amerenda/blog, React 18 + Vite 5
namespace: apps, hostname: blog.amer.dev, port 3000
markdown currently: string split in PostPage.tsx (brittle)
last deployed: 2026-04-10 via praetor task #87
```

The planner pre-fills the entire `modify_app` spec from this. No clarifying questions.

---

## Dispatch Path

Currently OWU calls `lm_praetor_dispatch` → `POST /api/v1/dispatch`.

With the spec layer, OWU calls `lm_praetor_execute_spec` → `POST /api/v1/spec/execute`.

### New endpoint: `POST /api/v1/spec/execute`

In `webhooks/spec.py` (new file):

```python
class SpecExecuteRequest(BaseModel):
    spec_toml: str

@router.post("/api/v1/spec/execute", dependencies=[Depends(_check_auth)])
async def execute_spec(req: SpecExecuteRequest, background_tasks: BackgroundTasks):
    spec = tomllib.loads(req.spec_toml)
    task_type = spec["task"]["type"]

    if task_type == "new_app":
        # Build AppPlan from spec fields and call existing app_factory logic
        plan = AppPlan(
            name=spec["repos"]["primary"].split("/")[1],
            description=spec["task"]["description"],
            domain=spec["infra"].get("hostname"),
            port=spec["infra"].get("port", 8000),
            stateless=spec["infra"].get("type") == "stateless",
        )
        # Delegate to existing handler — no duplication
        return await create_app(plan, background_tasks)

    elif task_type in ("modify_app", "fix_pr", "modify_pr"):
        # Embed spec TOML in task description, dispatch agent:code
        description = req.spec_toml
        task_id = int(time.time())
        dispatch_agent(task_id, spec["task"]["title"], description, "code")
        return {"task_id": task_id, "event": "agent:code"}
```

`tomllib` is stdlib in Python 3.11+. No new dependency.

---

## Coder Worker Changes

`agents/coder/worker.py` gains a spec parser. When a `[task]` block is present
in `task_description`, the structured fields drive the prompt. Falls back to
current `repo:` parsing otherwise — fully backwards compatible.

### New helper: `_parse_spec(description: str) -> dict | None`

```python
import tomllib, re

def _parse_spec(description: str) -> dict | None:
    match = re.search(r"```toml\n(.*?)```", description, re.DOTALL)
    if not match:
        return None
    try:
        return tomllib.loads(match.group(1))
    except tomllib.TOMLDecodeError:
        return None
```

### Spec-aware prompt in `_run_coder`

When `spec` is not None:

```python
spec = _parse_spec(input.task_description)

if spec:
    repo          = spec["repos"]["primary"]
    task_type     = spec["task"]["type"]
    prior_context = "\n".join(spec["context"].get("prior_memory", []))
    request_limit = spec["dispatch"].get("request_limit", 50)

    features = spec.get("app", {}).get("features") or spec.get("app", {}).get("changes") or []
    pr_number = spec.get("pr", {}).get("number")
    branch    = spec.get("pr", {}).get("branch")
    feedback  = spec.get("pr", {}).get("feedback", "")

    if task_type == "fix_pr":
        mode_block = (
            f"You are fixing PR #{pr_number} on branch {branch}.\n"
            f"DO NOT create a new branch or PR.\n"
            f"Review feedback to address:\n{feedback}\n"
        )
    elif task_type == "modify_app":
        mode_block = (
            f"You are modifying an existing app. Create branch praetor-coder/task-{input.task_id}.\n"
            f"Changes required:\n" + "\n".join(f"- {c}" for c in features) + "\n"
        )
    else:  # new_app
        framework = spec.get("app", {}).get("framework", "")
        mode_block = (
            f"New app — Create branch praetor-coder/task-{input.task_id}.\n"
            f"Stack: {framework}, port {spec['infra'].get('port', 8000)}\n"
            f"Implement ALL of the following features completely — do not stub:\n"
            + "\n".join(f"- {f}" for f in features) + "\n"
        )
    # prior_context already populated from spec — skip search_memory for spec tasks
    # (still call it as a supplement for anything the planner missed)
    ...
```

The coder prompt becomes unambiguous. Every feature is an explicit checklist item.
The model spends tokens on code, not on interpreting requirements.

---

## Mem0 Rule: All Agents Write to `planner-global`

Every agent that completes a task writes a summary to two namespaces:

1. **Agent-scoped** (existing): `"coder-amerenda/blog"`, `"reviewer-amerenda/blog"`
2. **Planner-global** (new): `"planner-global"`

This is a one-line addition in each worker after the existing `add_memory` calls:

```python
# existing
await add_memory(f"Task #{input.task_id}: ...", memory_agent_id)

# new — feeds the planning conversation
await add_memory(
    f"{repo}: {input.task_title} completed. PR #{pr_url}. Stack: {framework}. "
    f"Hostname: {hostname}. Namespace: {namespace}.",
    "planner-global",
)
```

This is the mechanism that makes "rewrite the blog" work without asking "which blog."

---

## What Gets Built — Implementation Checklist

### `dean-mcp/praetor-mcp/`
- [ ] Add `lm_praetor_memory_search(query: str) -> str` tool (~15 lines)
- [ ] Add `lm_praetor_execute_spec(spec_toml: str) -> str` tool (~10 lines, calls `/api/v1/spec/execute`)
- [ ] Register both in the MCP server manifest

### `praetor/webhooks/spec.py` (new file)
- [ ] `POST /api/v1/spec/execute` — parse TOML, route to app_factory or dispatch (~50 lines)
- [ ] Register router in `webhooks/app.py`

### `praetor/agents/coder/worker.py`
- [ ] `_parse_spec(description)` helper (~15 lines)
- [ ] Spec-aware prompt builder in `_run_coder` (~40 lines)
- [ ] `request_limit` read from spec (overrides hardcoded 50)
- [ ] Write planner-global Mem0 summary on completion

### `praetor/agents/pr_reviewer/worker.py`
- [ ] Write planner-global Mem0 summary on completion

### `praetor/agents/research/worker.py`
- [ ] Write planner-global Mem0 summary on completion

### Langfuse
- [ ] `planner-system` prompt — the OWU planning system prompt (see below)

### Tests
- [ ] `_parse_spec` round-trips all three task types
- [ ] `new_app` spec routes to app_factory handler
- [ ] `fix_pr` spec produces correct mode_block in coder prompt
- [ ] `lm_praetor_memory_search` returns formatted string from Mem0 mock
- [ ] `POST /api/v1/spec/execute` with bad TOML returns 422

---

## OWU Planner System Prompt (`planner-system` in Langfuse)

Key rules to include:

```
You are the Praetor planning assistant. You help the user dispatch coding tasks
to the Praetor agent platform. You do NOT write code yourself.

BEFORE asking the user any questions:
1. Call lm_praetor_memory_search with their message as the query.
   If results contain the answer to any clarifying question, skip that question.

REQUIRED FIELDS before producing a spec:
- new_app: repo name, hostname, framework, features list
- modify_app: which repo (check memory first), list of changes
- fix_pr: which repo, PR number, branch name

SPEC PRODUCTION:
When you have all required fields, produce a spec in a toml code block and
present it to the user with a plain-English summary of what will happen.
Do not call lm_praetor_execute_spec until the user explicitly approves.

APPROVAL SIGNALS: "yes", "go", "looks good", "ship it", "do it", thumbs up.

After dispatch, return the Hatchet task URL from lm_praetor_execute_spec.

DEFAULTS (use if user does not specify):
- framework: react-vite for frontend, python-fastapi for backend
- port: 3000 for Node, 8000 for Python
- namespace: apps
- redispatch_cap: 3 for new_app, 2 for modify_app, 1 for fix_pr
- request_limit: 80 for new_app, 60 for modify_app, 40 for fix_pr
```

---

## Pipeline Hand-off Clarification

The `agents` field in the spec (`agents = ["app_factory", "coder"]`) does NOT mean
Praetor dispatches both events simultaneously at spec-execute time. The hand-off works
like the existing `pipelines/research_then_code.py` pattern:

- `app_factory` is the first step. `POST /api/v1/spec/execute` calls the `create_app`
  handler from Phase 16, which already dispatches `agent:code` from inside
  `_provision_and_dispatch` after infra provisioning completes.
- The `agents` list in the spec is read by `POST /api/v1/spec/execute` to know whether
  to call `create_app` (which chains to coder automatically) or to dispatch `agent:code`
  directly (for `modify_app` / `fix_pr`).
- For `new_app`, the spec executor calls `create_app`. The coder dispatch is the
  responsibility of `_provision_and_dispatch` once infra is ready — same as today.
  No change to that code.

**"Praetor" in this context:** Praetor is the platform. All events (`agent:code`,
`agent:scaffold`, `pipeline:*`) are Praetor events handled by Praetor workers. The
distinction above is purely about which internal function triggers the next step:
the spec executor itself (for direct dispatches) vs. the app_factory background task
(for new_app, where the coder must wait for infra to be ready before it can run).

---

## What This Does NOT Solve

- **Multi-repo changes** — "update the blog AND add a backend API route" touches two
  repos. The spec currently has one `repos.primary`. Extension to `repos.secondary[]`
  is a future phase.
- **Spec validation pre-dispatch** — calling infra-mcp to check if the hostname is
  already taken, or if the repo already exists, before dispatch. Currently the
  `create_app` handler returns 409 if the repo exists; a pre-flight check in the spec
  executor is a nice-to-have.
- **Coder execution quality** — a precise spec tells the coder exactly what to build.
  Whether it builds it correctly is addressed separately in Phase 28 (coder quality
  improvements: smarter tool usage, test-first loop, implementation verification).
