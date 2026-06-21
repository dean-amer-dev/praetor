# Phase 16 — Full App Pipeline

**Goal:** From an OpenWebUI conversation, go from "here's what I want to build" to a running UAT deployment with CI/CD fully wired — without touching the terminal. This phase adds the two missing pieces the coder agent currently can't do on its own: (1) create a new GitHub repo from the app-template, and (2) provision CI runners for it. After this phase, the path from idea to UAT is fully automated.

## Pre-conditions

- Phase 15 complete (MCP factory stable — platform confirmed stable before adding app creation)
- `infra-mcp scaffold_app` and `open_deploy_pr` working (Phase 0)
- `infra-mcp add_mac_mini_runner` available (Phase 0)
- Coder agent working end-to-end (Phase 6)
- PR reviewer working (Phase 7)
- QA agent working (Phase 7)
- `app-template` repo exists at `amerenda/app-template` with CI skeleton

## The Gap Today

The coder agent writes code and opens PRs on **existing repos** only. To build a brand-new app you currently need to manually:

1. Create a GitHub repo (from `app-template`)
2. Add runners to that repo (via `infra-mcp add_mac_mini_runner`)
3. Create the repo's k3s manifests (UAT + prod) in `k3s-dean-gitops`
4. Then give the coder agent a task on the new repo

This phase collapses steps 1-3 into a single automated flow triggered from OpenWebUI.

## Architecture

```
OpenWebUI conversation
    │
    │  User: "Build an app that does X"
    │  Model: generates AppPlan, asks for approval
    │  User: approves
    │  Model: calls praetor_mcp.create_app(plan)
    │
    ▼
POST /api/v1/app/create
    │
    ├─► Create GitHub repo from app-template (GitHub API)
    │   (praetor-coder app, org-level repo creation)
    │
    ├─► infra-mcp scaffold_app → k3s manifests for UAT + prod
    │   infra-mcp open_deploy_pr → ArgoCD ready for UAT
    │
    ├─► infra-mcp add_mac_mini_runner → CI runners on new repo
    │
    └─► Dispatch coder agent:
        POST /api/v1/dispatch { type: "code", title: plan.title,
                                description: plan.full_description,
                                repo: plan.repo_name }
            │
            ▼
        Coder agent writes initial code, opens PR
            │
            ▼
        GitHub PR event → reviewer agent fires automatically
            │
            ▼
        PR merged → CI builds image → UAT manifest updated → ArgoCD syncs
            │
            ▼
        QA agent runs against UAT endpoint
            │
            ▼
        Prod deploy PR created → human approves → prod rolls
```

## What Gets Built

### 19a — AppPlan Schema

```python
class AppPlan(BaseModel):
    name: str                          # kebab-case, becomes repo name
    description: str                   # what the app does (for coder prompt)
    domain: str | None = None          # e.g. "myapp.amer.dev" (optional)
    port: int = 8000
    has_database: bool = False         # postgres via app-factory pattern
    env_secrets: dict[str, str] = {}   # {ENV_VAR: bws-secret-name}
    stateless: bool = True             # False = Komodo stateful (not yet automated)
```

### 19b — Repo Creation

Use the GitHub API with the praetor-coder GitHub App installation token to create a new repo from `app-template`:

```
POST /repos/amerenda/app-template/generate
{
  "owner": "amerenda",
  "name": "<name>",
  "private": false,
  "description": "<description>"
}
```

The `praetor-coder` GitHub App needs `administration:write` permission at org level for this. Check and grant if not already set.

### 19c — Runner Provisioning

Call `infra-mcp add_mac_mini_runner` for the new repo immediately after creation. This registers an ARC runner scale set on the mac-mini so the new repo's CI has a runner.

### 19d — k3s Manifests via infra-mcp

Reuse the existing `scaffold_app` + `open_deploy_pr` pattern from infra-mcp. The factory calls these with the `AppPlan` fields. ArgoCD will have the UAT namespace ready before the coder agent's first PR merges.

### 19e — Coder Dispatch with Full Plan Context

The coder agent currently receives `task_title` and `task_description`. For a new-app task, `task_description` includes the full `AppPlan` as structured context:

```
App: <name>
Repo: https://github.com/amerenda/<name>
Branch: praetor-coder/initial-implementation
Purpose: <description>
Port: <port>
Secrets needed: <env_secrets>
UAT URL: https://<name>-uat.amer.dev
```

The coder agent clones the new repo (app-template skeleton), implements the app, opens a PR. From that point, the existing reviewer → CI → UAT → QA → prod chain handles everything automatically.

### 19f — praetor_mcp Tool

Expose `create_app(plan: AppPlan)` as a tool in the `praetor-mcp` MCP server so OpenWebUI/qwen3-35b-think can call it directly from chat. The model creates the plan conversationally, asks for approval, then calls `create_app` with the approved spec.

### 19g — Plan Approval Step

The model should always present a plan and wait for explicit approval before calling `create_app`. This is enforced via the system prompt on qwen3-35b-think in OpenWebUI:

```
When a user asks you to build an app:
1. Gather requirements through conversation (2-3 exchanges max)
2. Present a structured AppPlan summary for approval
3. Wait for explicit "yes" / "looks good" / "go ahead" before calling create_app
4. Never call create_app without explicit approval
```

## Phase 16 Ready Conditions

1. User describes an app in OpenWebUI → model creates a plan and waits for approval (does not auto-fire)
2. User approves → `create_app` called → GitHub repo created within 30s
3. Runner provisioned on new repo → CI can execute within 5 minutes of repo creation
4. k3s UAT manifests created via infra-mcp → ArgoCD has the namespace before first CI run
5. Coder agent opens a PR on the new repo with working initial code
6. PR reviewer fires automatically on the PR
7. PR merged → CI builds → UAT pod running (check with `get_app_status`)
8. QA agent runs against the UAT endpoint and posts results
9. Prod deploy PR created → after human merge, prod pod running
10. Entire flow from "user approves plan" to "UAT running" completes in under 15 minutes
