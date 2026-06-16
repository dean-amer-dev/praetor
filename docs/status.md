# Praetor Platform — Phase Status

| Phase | Name | Status | Notes |
|-------|------|--------|-------|
| 0 | Deployment Foundation | ✅ Complete | infra-mcp, bws-mcp, GitHub apps deployed |
| 1 | Inference | ✅ Complete | LiteLLM at litellm.amer.dev |
| 2 | Storage | ✅ Complete | Qdrant on Mac Mini via Komodo |
| 3 | Dispatch | ✅ Complete | Hatchet Lite on k3s, stub worker |
| 4 | Memory | ✅ Complete | mem0 arm64 image healthy; smoke + E2E verified (Phase 12a) |
| 5 | Research Agent | ✅ Complete | Vikunja ai-research label → research worker |
| 6 | Coder Agent | ✅ Complete | Vikunja ai-go label → coder worker, PRs via dean-coder[bot] |
| 7 | PR Reviewer + QA | ✅ Complete | End-to-end verified via test PR #36; amerenda-reviewer[bot] posts reviews |
| 8 | Multi-Agent Pipeline | ✅ Complete | Dual-label tasks → pipeline:research_code DAG; pipeline-worker deployed |
| 9 | Observability + Prompts | ✅ Complete | Langfuse deployed; @observe() on all tools; eval scores wired; coder-system v4 prompt live; real Hatchet run traced with tool call spans (trace 7c8cb711) |
| 10 | MCP Gateway | ✅ Complete | mcp-searxng + LiteLLM gateway live; agents wired; 2 tools verified at /mcp endpoint |
| 11 | Scaffold Worker | ✅ Complete | agent:scaffold event; Jinja templates; draft PRs on praetor + dean-mcp from OpenWebUI |
| 12 | Platform Verification | ✅ Complete | All smoke tests pass (18/18); health check green; Phase 4+7 verified; OOM fix + MCP URL fix merged (k3s-dean-gitops #802) |
| 13 | Agent Benchmarking & Eval | 🔵 In Progress | benchmark-worker + eval datasets + runner script built; pending: run datasets, record baselines, deploy |
| 14 | Voice Dispatch | ⬜ Not started | Requires Phase 13 |
| 15 | Control Plane UI | ⬜ Not started | Requires Phase 14 |

## OpenWebUI (claw.amer.dev)

Admin credentials reset on 2026-06-13. Login with:
- **URL:** https://claw.amer.dev
- **Email:** `amerenda@proton.me`
- **Password:** stored in BWS as `openwebui-dean-admin-password`

---

## Completed Work (Pre-Phase 12)

### Conversational Dispatch (completed prior to Phase 12 renumbering)
- `POST /api/v1/dispatch` and `GET /api/v1/status/{task_id}` live on praetor webhook-adapter
- `praetor-mcp` deployed at `praetor-mcp.amer.dev` — MCP tools for Claude Code and opencode
- OpenWebUI dispatch tool registered at claw.amer.dev
- `common/dispatch.py` shared dispatch function; vikunja.py refactored to use it
- PRs merged: praetor#35, dean-mcp#17, k3s-dean-gitops#800

### Phase 7 — Webhook Setup
The `amerenda-reviewer` GitHub App is installed on all repos in the org and delivers
`pull_request` events to `https://pubhooks.amer.dev/praetor/webhooks/github` (public
ingress via Traefik, strips `/praetor` prefix, routes to praetor-webhook-adapter).

No org-level webhook needed. No manual curl. End-to-end flow still needs verification (Phase 12b).
