# Phase 18 — Kubernetes MCP

**Goal:** Use the Phase 19 Intelligent MCP Agent to discover and deploy `mcp-server-kubernetes`, making it available to praetor agents with two access tiers: read-only (safe for diagnostic agents) and read-write (for operational tasks like scaling the benchmark-worker). This phase is the first real-world test of the intelligent MCP pipeline.

## Pre-conditions

- Phase 17 complete (Intelligent MCP Agent — `POST /api/v1/mcp/request` working)
- Phase 15 complete (MCP factory deployment API)
- k3s cluster accessible from MCP pods (in-cluster service accounts)

## How This Phase Is Executed

Rather than manually calling `POST /api/v1/mcp/register` as was done in earlier phases, Phase 20 is triggered via the intelligent agent:

```
POST /api/v1/mcp/request
{ "capability": "query and manage Kubernetes cluster resources — pods, deployments, logs, scaling" }
```

The research agent finds `ghcr.io/flux159/mcp-server-kubernetes` (high confidence), the factory registers it, and the two instances below are deployed. This validates the full Phase 17 pipeline end-to-end.

## Implementation

### 20a — Deploy Read-Only Instance

Via Phase 17 intelligent agent (research → register):

```json
POST /api/v1/mcp/register
{
  "name": "kubernetes-readonly",
  "image": "ghcr.io/flux159/mcp-server-kubernetes:latest",
  "port": 3000,
  "transport": "http",
  "env_secrets": {}
}
```

Uses in-cluster service account with `get`/`list`/`watch` RBAC on the praetor namespace. Tools available: `kubectl_get`, `kubectl_describe`, `kubectl_logs`, `kubectl_events`, `explain_resource`.

### 20b — Deploy Read-Write Instance

```json
POST /api/v1/mcp/register
{
  "name": "kubernetes-rw",
  "image": "ghcr.io/flux159/mcp-server-kubernetes:latest",
  "port": 3000,
  "transport": "http",
  "env_secrets": {}
}
```

Full access: `kubectl_scale`, `kubectl_apply`, `kubectl_patch`, `kubectl_rollout`. Scoped to the praetor namespace only.

### 20c — Service Accounts

Two k3s service accounts provisioned alongside the MCP deployments:
- `mcp-kubernetes-readonly-sa` — ClusterRole: `view` (built-in)
- `mcp-kubernetes-rw-sa` — ClusterRole: custom, `get`/`list`/`watch`/`update`/`patch` on `deployments`, `pods`, `services` in the `praetor` namespace only

### 20d — Benchmark Worker Scale Flow

Replace the current manual `kubectl scale` workaround. When running benchmarks:

1. Claude calls `kubernetes-rw` tool `kubectl_scale deployment/praetor-benchmark-worker --replicas=1 -n praetor`
2. Dispatches benchmark events
3. Polls until benchmarks complete
4. Scales back down to 0

### 20e — Wire into Agent Context

Add `kubernetes-readonly` to the research agent's MCP server list in LiteLLM. The read-write instance is invoked explicitly only (by Claude Code or a platform operator, not autonomously by praetor agents).

## Phase 18 Ready Conditions

1. `POST /api/v1/mcp/request` with kubernetes capability → research agent finds the image, factory PR opened (validates Phase 19)
2. `kubernetes-readonly` deployed and healthy; `kubectl_get pods -n praetor` returns current pod list
3. `kubernetes-rw` deployed and healthy; `kubectl_scale` successfully scales a deployment
4. Benchmark-worker scale-up/down works end-to-end via `kubernetes-rw` (replicas 0→1→0)
5. Research agent can call `kubectl_logs` and `kubectl_describe` via `kubernetes-readonly` during a task
6. Neither MCP instance can affect namespaces outside `praetor` (RBAC verified)
