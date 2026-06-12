# Praetor Platform — Status

Last updated: 2026-06-12

## Phase Status

| Phase | Name | Status | Notes |
|-------|------|--------|-------|
| 0 | Deployment Foundation | ✅ Complete | infra-mcp, bws-mcp deployed; GitHub App `amerenda-coder` installed |
| 1 | Inference | ✅ Complete | litellm.amer.dev live; `coder` alias → qwen3-35b on murderbot:8088 |
| 2 | Storage | ✅ Complete | Qdrant on Mac Mini core stack |
| 3 | Dispatch | ✅ Complete | hatchet.amer.dev live; stub worker connected |
| 4 | Memory | ✅ Complete | mem0.amer.dev live; pgvector backend |
| 5 | Research Agent | ✅ Complete | praetor-research-worker Running; ai-research label → agent:research |
| 6 | Coder Agent | ⚠️ Deployed, fix pending | Pod in CreateContainerConfigError — runAsNonRoot + root image. PRs open: praetor#5 (Dockerfile), k3s-dean-gitops#726 (securityContext). Merge both to unblock. |
| 7 | PR Reviewer + QA | 🔲 Not started | |
| 8 | Multi-Agent Pipeline | 🔲 Not started | |
| 9 | Observability + Prompts | 🔲 Not started | Langfuse |
| 10 | MCP Gateway | 🔲 Not started | LiteLLM MCP Gateway |
| 11 | Scaffold Worker | 🔲 Not started | OpenWebUI → scriptor → PR |
| 12 | Manual | ⛔ Skip | Control Plane UI — implement manually |

## Current Blockers

### Phase 6 — Coder Worker Not Running

**Error:** `container has runAsNonRoot and image will run as root`

**Fix:**
1. Merge [praetor#5](https://github.com/amerenda/praetor/pull/5) — adds `appuser` (UID 1000) to `Dockerfile.coder-worker`
2. Merge [k3s-dean-gitops#726](https://github.com/amerenda/k3s-dean-gitops/pull/726) — adds pod-level `securityContext` with `runAsUser/fsGroup: 1000`
3. Image already rebuilt and pushed: `amerenda/praetor-coder:latest`

After both PRs merge and ArgoCD syncs, the pod will restart with the correct user context.

## Phase 6 Ready Conditions (verify after fix)

1. `kubectl get pods -n praetor` shows `praetor-coder-worker` in `Running` state
2. Create Vikunja task "Add /healthz endpoint to ecdysis", label `ai-go` (ID 11), description `repo: amerenda/ecdysis`
3. Within 30s: Hatchet UI shows `agent:code` run triggered
4. Draft PR opened on `amerenda/ecdysis` branch `amerenda-coder/task-{id}`, commits authored by `scriptor[bot]`
5. Vikunja task marked done, comment contains PR URL
6. Create a task with no `repo:` in description → graceful error comment on Vikunja, Hatchet run completes without exception

## Architecture Deviations from Spec

| Spec | Reality | Phase |
|------|---------|-------|
| `dean-coder` GitHub App | App renamed to `amerenda-coder`; display name `scriptor[bot]` | 6 |
| llama-server on port 8080 | Permanently on 8088 (`svclb-unifi-controller` holds 8080) | 1 |
| Vikunja webhook dedup via idempotency_key | Implemented via Hatchet ConcurrencyExpression per task_id instead | 5, 6 |
| Mem0 with Qdrant backend | pgvector only (Mem0 OSS server hardcodes pgvector) | 4 |
