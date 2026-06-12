# Phase 9 — Observability + Prompt Management: Langfuse

**Goal:** Prompts are versioned and editable in a UI without redeploying agents. Every agent run produces a trace with full tool call chain, token counts, and latency. Evaluations can be run against any prompt version or model.

## Pre-conditions

- Phases 1–6 complete and stable
- Mac Mini PostgreSQL healthy (Langfuse uses a Postgres database)

## Why Langfuse

Three problems it solves at once:

1. **Prompt management** — system prompts live in Langfuse's DB, not in Python source files. Agents fetch the current production prompt at startup. Changing a prompt is a UI action, not a PR.
2. **Tracing** — every Hatchet task run becomes a Langfuse trace: input payload → tool calls (with arguments and results) → final output, plus token counts and wall-clock latency per step. No guessing what the agent did on a given run.
3. **Evaluations** — define a dataset of tasks with expected outputs. Run the dataset against any combination of (model, prompt version). Score the results. This is how you benchmark "does the agent correctly diagnose a kubectl error" — build a ground-truth set, run it, compare versions.

Langfuse is MIT-licensed and self-hostable. It uses PostgreSQL (no extra stateful services needed) and runs as a single stateless container on k3s.

## What Gets Built

### Provisioning Langfuse

Langfuse needs a PostgreSQL database. Provision via app-factory:

```
scaffold_app("langfuse", "LLM observability, prompt management, and evals", "stateless")
# Edit app-factory/apps/langfuse.toml:
#   component: image langfuse/langfuse:latest, port 3000
#   secrets: NEXTAUTH_SECRET (generate=true), SALT (generate=true), LANGFUSE_INIT_ORG_ID (generate=false)
#   database: name "langfuse", host "10.100.20.18"
provision_app("langfuse")
open_deploy_pr("langfuse", "phase-9: deploy Langfuse observability platform")
```

**Key env vars:**

| Env Var | Source | Value |
|---------|--------|-------|
| `DATABASE_URL` | ExternalSecret | `postgresql://langfuse:<pw>@10.100.20.18:5432/langfuse` |
| `NEXTAUTH_URL` | ConfigMap | `https://langfuse.amer.dev` |
| `NEXTAUTH_SECRET` | ExternalSecret | auto-generated |
| `SALT` | ExternalSecret | auto-generated |
| `LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES` | ConfigMap | `true` |

**DNS:** `langfuse.amer.dev` → k3s ingress → Langfuse pod (port 3000)

### Integrating Agents with Langfuse

#### 1. Prompt management — fetch at startup, not hardcoded

```python
from langfuse import Langfuse

_langfuse = Langfuse(
    host=os.environ["LANGFUSE_HOST"],       # https://langfuse.amer.dev
    public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
    secret_key=os.environ["LANGFUSE_SECRET_KEY"],
)

def get_system_prompt(name: str) -> str:
    """Fetch the production-labeled prompt from Langfuse."""
    return _langfuse.get_prompt(name).compile()
```

The agent calls `get_system_prompt("coder-system")` at startup. To change the prompt: edit it in the Langfuse UI → click "Promote to production" → next agent restart picks it up. No PR, no image rebuild.

#### 2. Tracing — wrap each Hatchet task

```python
from langfuse import Langfuse
from langfuse.decorators import langfuse_context, observe

@observe()
async def _run_coder(input: CoderInput, context: Context) -> dict:
    langfuse_context.update_current_trace(
        name=f"coder-task-{input.task_id}",
        input=input.model_dump(),
        tags=["coder", f"task-{input.task_id}"],
    )
    # ... agent.run(prompt) ...
    langfuse_context.update_current_trace(output=result.data)
    return {"result": result.data}
```

PydanticAI's Langfuse callback handler captures all tool calls automatically — each tool invocation appears as a child span with arguments and return value.

#### 3. Evaluations

Define a dataset in Langfuse UI (or via SDK):

```python
langfuse.create_dataset("kubectl-diagnose")
langfuse.create_dataset_item(
    dataset_name="kubectl-diagnose",
    input={"task_title": "Pod CrashLoopBackOff", "task_description": "repo: amerenda/ecdysis"},
    expected_output={"contains": ["CrashLoopBackOff", "logs", "kubectl describe"]},
)
```

Run the dataset against the current agent (or a specific prompt version). Score each output. Compare scores across model or prompt changes in the Langfuse UI dashboard.

### New Secrets in ExternalSecret (coder-worker, research-worker)

```yaml
- secretKey: langfuse-public-key
  remoteRef:
    key: langfuse-public-key
    property: password
- secretKey: langfuse-secret-key
  remoteRef:
    key: langfuse-secret-key
    property: password
```

New env vars on each worker pod:
```
LANGFUSE_HOST:       https://langfuse.amer.dev
LANGFUSE_PUBLIC_KEY: ← from secret
LANGFUSE_SECRET_KEY: ← from secret
```

### Migrating Existing Hardcoded Prompts

On first deployment:
1. Copy `SYSTEM_PROMPT` strings from `agents/coder/agent.py` and `agents/research/agent.py` into Langfuse UI as new prompts named `coder-system` and `research-system`
2. Label them `production`
3. Update agent code to call `get_system_prompt()` at startup
4. Delete the hardcoded strings

After this, prompts are owned by Langfuse. The Python files only contain tool definitions and agent wiring.

## Ready Conditions for Phase 9

1. `https://langfuse.amer.dev` loads, login works
2. `coder-system` and `research-system` prompts exist in Langfuse, labeled `production`
3. Trigger a coder Hatchet run → trace appears in Langfuse with full tool call chain
4. Edit the `coder-system` prompt in Langfuse UI → restart coder-worker pod → next run uses the new prompt
5. Create a 3-task eval dataset → run it → scores visible in Langfuse Evals dashboard
