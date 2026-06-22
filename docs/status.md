# Praetor Platform — Phase Status

| Phase | Name | Status | Notes |
|-------|------|--------|-------|
| 0 | Deployment Foundation | ✅ Complete | infra-mcp, bws-mcp, GitHub apps deployed |
| 1 | Inference | ✅ Complete | LiteLLM at litellm.amer.dev |
| 2 | Storage | ✅ Complete | Qdrant on Mac Mini via Komodo |
| 3 | Dispatch | ✅ Complete | Hatchet Lite on k3s, stub worker |
| 4 | Memory | ✅ Complete | mem0 arm64 image healthy; smoke + E2E verified (Phase 12a) |
| 5 | Research Agent | ✅ Complete | OWU dispatch → research worker (Vikunja trigger: outstanding) |
| 6 | Coder Agent | ✅ Complete | OWU dispatch → coder worker, PRs via praetor-coder[bot] (Vikunja trigger: outstanding) |
| 7 | PR Reviewer + QA | ✅ Complete | End-to-end verified via test PR #36; amerenda-reviewer[bot] posts reviews |
| 8 | Multi-Agent Pipeline | ✅ Complete | Dual-label tasks → pipeline:research_code DAG; pipeline-worker deployed |
| 9 | Observability + Prompts | ✅ Complete | Langfuse deployed; @observe() on all tools; eval scores wired; coder-system v4 prompt live; real Hatchet run traced with tool call spans (trace 7c8cb711) |
| 10 | MCP Gateway | ✅ Complete | mcp-searxng + LiteLLM gateway live; agents wired; 2 tools verified at /mcp endpoint |
| 11 | Scaffold Worker | ✅ Complete | agent:scaffold event; Jinja templates; draft PRs on praetor + dean-mcp from OpenWebUI |
| 12 | Platform Verification | ✅ Complete | All smoke tests pass (18/18); health check green; Phase 4+7 verified; OOM fix + MCP URL fix merged (k3s-dean-gitops #802) |
| 13 | OpenWebUI Integration | ✅ Complete | mcp-bridge exposes praetor_mcp + infra_mcp tools; system prompt set on qwen3-35b-think; ENABLE_MEMORIES+MEM0 env vars live; native Mem0 provider not in current OWU build (env pre-wired for when it lands) |
| 14 | Agent Benchmarking & Eval | ✅ Complete | benchmark-worker deployed; baseline run `baseline-1781706219` complete (10/10); research-eval mean=1.000, reviewer-eval mean=0.846 recorded in eval-baselines.md |
| 15 | Self-Service MCP Factory | ✅ Complete | POST /api/v1/mcp/register → GitOps PR on k3s-dean-gitops (deployment + service + ArgoCD app + LiteLLM mcp_servers); GET /api/v1/mcp lists from k8s ConfigMap registry; 19 unit tests |
| 16 | Full App Pipeline | ✅ Complete | POST /api/v1/app/create; GitHub repo from template + infra-mcp provision + coder dispatch; 34 unit tests |
| 17 | Intelligent MCP Agent | ✅ Complete | POST /api/v1/mcp/request + request_mcp MCP tool; registry dedup + LLM research + use_existing/scaffold_new routing; 22 unit tests |
| 18 | Kubernetes MCP | ✅ Complete | mcp-server-kubernetes deployed via Phase 17; github-mcp registered in LiteLLM |
| 22 | Coder Full GitHub API + OWU-Only Dispatch | 🔄 In Progress | `github_api` tool added; github-mcp → LiteLLM; Vikunja removed as primary trigger; OWU only |
| 23 | Coder Re-Dispatch Loop | ⬜ Not started | reviewer REQUEST_CHANGES → re-dispatch coder, cap 2 |
| 24 | Agent Factory | ⬜ Not started | POST /api/v1/agent/create → scaffold → deploy → smoke test in one call |
| 25 | Inline Arbitration | ⬜ Not started | loop exhausted → focused LLM call, decision memo to mem0 + PR |
| 26 | Voice Dispatch | ⬜ Not started | |
| 27 | Control Plane UI | ⬜ Not started | |

## OpenWebUI (bot.amer.dev)

Admin credentials reset on 2026-06-13. Login with:
- **URL:** https://bot.amer.dev
- **Email:** `amerenda@proton.me`
- **Password:** stored in BWS as `openwebui-dean-admin-password`

---

## Completed Work (Pre-Phase 12)

### Conversational Dispatch (completed prior to Phase 12 renumbering)
- `POST /api/v1/dispatch` and `GET /api/v1/status/{task_id}` live on praetor webhook-adapter
- `praetor-mcp` in dean-mcp exposes `lm_praetor_dispatch` to OpenWebUI — primary trigger
- OpenWebUI dispatch tool registered at bot.amer.dev (all agent invocations go through OWU)
- `common/dispatch.py` shared dispatch function used by all trigger paths
- PRs merged: praetor#35, dean-mcp#17, k3s-dean-gitops#800
- Vikunja webhook trigger code exists but is not active — outstanding for future use

### Phase 7 — Webhook Setup
The `amerenda-reviewer` GitHub App is installed on all repos in the org and delivers
`pull_request` events to `https://pubhooks.amer.dev/praetor/webhooks/github` (public
ingress via Traefik, strips `/praetor` prefix, routes to praetor-webhook-adapter).

No org-level webhook needed. No manual curl. End-to-end flow still needs verification (Phase 12b).
