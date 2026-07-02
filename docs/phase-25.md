# Phase 25 — Agent Factory

## You are implementing Phase 25 of the Praetor platform.

**You are allowed to merge PRs for this session.**

---

## Context

You are working on the `amerenda/praetor` monorepo. The platform runs on a k3s cluster (GitOps via `amerenda/k3s-dean-gitops`, synced by ArgoCD). Secrets come exclusively from Bitwarden Secrets Manager (BWS) — never hardcode secrets, never put them in Git, never create them anywhere except via BWS. The MCP server registry and tool exposure are managed via `amerenda/dean-mcp`.

### What already exists

- **Phase 11** — `agent:scaffold` Hatchet event → scaffold-worker opens draft PRs with agent skeletons on `amerenda/praetor`
- **Phase 16** — after CI builds an image, it opens a deploy PR on `k3s-dean-gitops`
- **Phase 15** — `POST /api/v1/mcp/register` deploys MCP servers end-to-end
- **Phase 17** — `lm_praetor_request_mcp` tool in praetor-mcp exposes MCP factory to OWU
- All existing agents (coder, research, reviewer, qa, scaffold) are running workers in the `praetor` k3s namespace

### What is missing

There is no way to go from "I want a new agent that does X" to a running, smoke-tested worker in one call. Currently each agent requires manual: scaffold dispatch → PR review → merge → CI → deploy PR → merge → Langfuse prompt creation → smoke test. Phase 25 automates all of it.

---

## Your constraints

**GitOps only.** Every change to running infrastructure must go through a PR on `k3s-dean-gitops` (for k3s) or `komodo-dean-gitops` (for Komodo/stateful). No `kubectl apply`, no direct edits to running pods, no one-off patches. If something can't be done via GitOps, that is a design problem to solve — not a reason to bypass the pipeline.

**BWS is the single source of truth for all secrets.** No exceptions. No secrets in Git. No secrets in env vars set manually. Every secret the new agent needs must be in BWS and pulled at runtime via the ExternalSecret/BWS provider already wired in the cluster.

**Ansible-playbooks for infrastructure only.** If a change requires installing a system package, configuring a node, or setting up a new host — and it truly cannot be done via k3s/Komodo — add it to `amerenda/ansible-playbooks`. Do not run ad-hoc commands on nodes.

**No auto-merge of production app PRs.** The agent factory itself auto-merges scaffold PRs and deploy PRs for new agents it creates (that's its job). But any PR that touches the existing praetor platform production config requires human review.

---

## What to build

### 1. `POST /api/v1/agent/create` in `webhooks/agent_factory.py`

```python
class AgentCreateRequest(BaseModel):
    name: str           # kebab-case, e.g. "grafana-monitor"
    description: str    # what it does, what tools it needs
    event: str          # Hatchet event name, e.g. "agent:grafana-monitor"
    tools: list[str] = []   # hint which shared tools to wire
```

The endpoint orchestrates the full lifecycle. It returns a status URL while work proceeds async via Hatchet.

**Lifecycle:**

1. Dispatch `agent:scaffold` with name/description/event/tools → scaffold-worker opens a draft PR on `amerenda/praetor` containing:
   - `agents/{name}/__init__.py`, `agent.py`, `worker.py`
   - `Dockerfile.{name}-worker`
   - Patch to webhook-adapter event routing (adds new event → worker mapping)
   - Patch to `.github/workflows/build.yaml` detect-changes matrix (adds new component)
2. Merge the scaffold PR (auto-merge — it is a known-good Jinja skeleton, not custom logic)
3. CI builds the image → opens a deploy PR on `k3s-dean-gitops` (existing CI pattern)
4. Merge the deploy PR → ArgoCD rolls out the new worker pod
5. Create a Langfuse system prompt named `{name}-system` via the Langfuse API using the existing `langfuse_tools.py` pattern
6. Smoke test: dispatch `{"title": "smoke-{name}", "description": "Respond OK.", "type": name}` via Hatchet, poll up to 60s for a clean completion
7. Return `{ agent, event, pod_status, langfuse_prompt_url, smoke_test }`

**Idempotency:** If `name` already exists in the k8s namespace as a running deployment, skip scaffold + deploy and return current status.

### 2. `lm_praetor_create_agent` tool in `dean-mcp/praetor-mcp/server.py`

Expose the new endpoint as an MCP tool at the praetor-mcp server, following the same pattern as `lm_praetor_dispatch` and `lm_praetor_request_mcp`. This makes agent creation available from any OWU conversation.

```python
def lm_praetor_create_agent(name: str, description: str, event: str, tools: list[str] = []) -> dict
```

### 3. Unit tests

Add tests in `tests/unit/test_agent_factory.py` covering:
- Idempotent name check
- Scaffold dispatch payload construction
- Langfuse prompt name derivation

---

## Deployment

Both `praetor` (webhook-adapter) and `dean-mcp` (praetor-mcp) deploy via their existing CI pipelines. When you open PRs on those repos, CI builds images and opens deploy PRs on `k3s-dean-gitops`. Merge those deploy PRs to get the changes live.

All secrets needed by new agents must be created in BWS first, then referenced in the scaffold template's ExternalSecret. Do not invent new secrets — use existing ones where the pattern already exists (e.g. `hatchet-worker-api-token`, `litellm-master-key`).

---

## Done when

1. `POST /api/v1/agent/create` with `name="phase25-canary"`, a description, and `event="agent:phase25-canary"` completes within 5 minutes and returns `smoke_test: "passed"`
2. The new agent's k3s Deployment exists in the `praetor` namespace — pod is `Running`
3. ArgoCD shows the `praetor` application as `Healthy` and `Synced`
4. `lm_praetor_create_agent` is listed in the praetor-mcp tool manifest (`GET /mcp/tools`)
5. praetor-mcp pod in k3s is `Running`, ArgoCD application is `Healthy` and `Synced`
6. A second call with `name="phase25-canary"` is idempotent — returns current status, opens no new PRs
7. `GET https://praetor.amer.dev/api/v1/agent/phase25-canary` returns the agent's registered state
