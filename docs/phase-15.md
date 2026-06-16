# Phase 15 — Kubernetes MCP

**Goal:** Deploy `Flux159/mcp-server-kubernetes` via the Phase 14 MCP factory and make it available to praetor agents with two access tiers: read-only (safe for diagnostic agents) and read-write (for operational tasks like scaling the benchmark-worker).

## Pre-conditions

- Phase 14 complete (self-service MCP factory working end-to-end)
- `POST /api/v1/mcp/register` tested and stable
- k3s cluster accessible from the MCP pod (in-cluster service account)

## Implementation

### 15a — Deploy Read-Only Instance

Register via Phase 14 factory:

```json
POST /api/v1/mcp/register
{
  "name": "kubernetes-readonly",
  "image": "ghcr.io/flux159/mcp-server-kubernetes:latest",
  "port": 3000,
  "transport": "http",
  "args": [],
  "env_secrets": {},
  "env": {
    "ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS": "true"
  }
}
```

Uses in-cluster service account with `get`/`list`/`watch` RBAC on praetor namespace. Tools available: `kubectl_get`, `kubectl_describe`, `kubectl_logs`, `kubectl_events`, `explain_resource`.

### 15b — Deploy Read-Write Instance

```json
POST /api/v1/mcp/register
{
  "name": "kubernetes-rw",
  "image": "ghcr.io/flux159/mcp-server-kubernetes:latest",
  "port": 3000,
  "transport": "http",
  "env_secrets": {},
  "env": {}
}
```

Full access: includes `kubectl_scale`, `kubectl_apply`, `kubectl_patch`, `kubectl_rollout`. Scoped to the praetor namespace. Used when Claude needs to scale the benchmark-worker or restart a deployment.

### 15c — Service Accounts

Two k3s service accounts provisioned by the MCP factory:
- `mcp-kubernetes-readonly-sa` — ClusterRole: `view` (built-in)
- `mcp-kubernetes-rw-sa` — ClusterRole: custom, allows `get`/`list`/`watch`/`update`/`patch` on `deployments`, `pods`, `services` in the `praetor` namespace only

Note: Full RBAC design deferred — Phase 15 uses the minimal set needed for the benchmark-worker scale use case.

### 15d — Benchmark Worker Scale Flow

Replace the current `kubectl scale` workaround. When running benchmarks:

1. Claude calls `kubernetes-rw` MCP tool `kubectl_scale`:
   ```
   deployment/praetor-benchmark-worker --replicas=1 -n praetor
   ```
2. Dispatches benchmark events
3. Polls until benchmarks complete
4. Scales back down:
   ```
   deployment/praetor-benchmark-worker --replicas=0 -n praetor
   ```

No more manual kubectl commands for benchmark runs.

### 15e — Wire into Agent Context

Add `kubernetes-readonly` to the diagnostic/research agent's MCP server list in LiteLLM. The read-write instance is only invoked explicitly (by Claude Code, not autonomously by praetor agents).

## Phase 15 Ready Conditions

1. `kubernetes-readonly` MCP deployed and healthy; `kubectl_get pods -n praetor` returns current pod list
2. `kubernetes-rw` MCP deployed and healthy; `kubectl_scale` successfully scales a deployment
3. Benchmark-worker scale-up/down works end-to-end via `kubernetes-rw` MCP (replicas 0→1→0)
4. Research agent can call `kubectl_logs` and `kubectl_describe` via `kubernetes-readonly` during a task
5. Neither MCP instance can affect namespaces outside `praetor` (RBAC verified)
