# Phase 15 — Self-Service MCP Factory

**Goal:** Automate the two-step manual process for adding a new MCP to praetor: deploy the MCP server as a k3s service and register it in the LiteLLM gateway. After this phase, adding an MCP is a single API call, not a PR + configmap edit + restart cycle.

## Pre-conditions

- Phase 14 complete (platform stable, benchmarks established)
- infra-mcp `scaffold_app` + `open_deploy_pr` working (Phase 0)
- LiteLLM configmap pattern established (Phase 10)
- github-mcp available for automated PR creation

## The Gap Today

Adding an MCP today requires:
1. Manually write a k3s Deployment + Service + ArgoCD Application
2. Open a k3s-dean-gitops PR, wait for merge, wait for ArgoCD sync
3. Edit `apps/litellm/server/configmap.yaml` `mcp_servers:` section
4. Open another PR, merge, LiteLLM reloads

Two PRs, two ArgoCD syncs, fully manual. This phase collapses it to one command.

## Architecture

```
POST /api/v1/mcp/register
  { name, image, port, transport, env_secrets }
          │
          ├─► infra-mcp scaffold_app → k3s Deployment + Service
          │   infra-mcp open_deploy_pr → merge → ArgoCD deploys pod
          │
          └─► patch litellm configmap mcp_servers:
              open PR on k3s-dean-gitops → merge → LiteLLM reloads
```

## What Gets Built

### 14a — MCP Spec Schema

A pydantic model for an MCP registration request:

```python
class McpRegistration(BaseModel):
    name: str                        # e.g. "kubernetes-readonly"
    image: str                       # Docker Hub image
    port: int = 8000                 # container port the MCP listens on
    transport: str = "http"          # "http" or "stdio"
    env_secrets: dict[str, str] = {} # {ENV_VAR: bws-secret-name}
    args: list[str] = []             # container args (e.g. ["--read-only"])
```

### 14b — Registration Endpoint

New route in `webhooks/app.py`:

```
POST /api/v1/mcp/register   → register + deploy a new MCP
GET  /api/v1/mcp            → list registered MCPs and their status
DELETE /api/v1/mcp/{name}   → remove an MCP (undeploy + deregister)
```

Handler calls the scaffold + LiteLLM patch logic in sequence. Returns a `task_id` that tracks the GitOps PR chain.

### 14c — k3s Deployment via infra-mcp

Reuse the existing scaffold pattern. The factory generates:
- `apps/mcp/<name>/deployment.yaml`
- `apps/mcp/<name>/service.yaml`
- ArgoCD Application wired into root-app.yaml at sync-wave 4

All MCPs live under `apps/mcp/` namespace in k3s-dean-gitops with their own namespace `mcp-<name>`.

### 14d — LiteLLM Configmap Patch

After the k3s service is deployed, patch the LiteLLM configmap to add:

```yaml
mcp_servers:
  <name>:
    url: "http://<name>-server.mcp-<name>.svc.cluster.local:<port>/mcp"
    transport: "<transport>"
```

Done via a commit to k3s-dean-gitops (same PR as the service manifests, or a follow-up PR). LiteLLM picks it up on next pod restart or config reload.

### 14e — MCP Registry State

Track registered MCPs in a ConfigMap in the `praetor` namespace so `GET /api/v1/mcp` can return live state without hitting the git repo on every request.

## Phase 15 Ready Conditions

1. `POST /api/v1/mcp/register` with a test MCP image → k3s pod Running within 5 minutes
2. LiteLLM `/mcp/` endpoint exposes the new MCP's tools within 5 minutes of registration
3. `GET /api/v1/mcp` returns the registered MCPs with correct status
4. Re-registering an existing MCP name returns a clear error (no duplicate deployments)
5. An agent calling `POST /api/v1/dispatch` with a task that uses the new MCP tool executes successfully
