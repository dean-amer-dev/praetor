# Praetor Platform — Phase Status

| Phase | Name | Status | Notes |
|-------|------|--------|-------|
| 0 | Deployment Foundation | ✅ Complete | infra-mcp, bws-mcp, GitHub apps deployed |
| 1 | Inference | ✅ Complete | LiteLLM at litellm.amer.dev |
| 2 | Storage | ✅ Complete | Qdrant on Mac Mini via Komodo |
| 3 | Dispatch | ✅ Complete | Hatchet Lite on k3s, stub worker |
| 4 | Memory | 🟡 Verify | mem0 switched to official arm64 image on RPi nodes (k3s-dean-gitops #757) — confirm health |
| 5 | Research Agent | ✅ Complete | Vikunja ai-research label → research worker |
| 6 | Coder Agent | ✅ Complete | Vikunja ai-go label → coder worker, PRs via dean-coder[bot] |
| 7 | PR Reviewer + QA | 🟡 Verify | Images built, webhook live at pubhooks.amer.dev — confirm end-to-end with test PR |
| 8 | Multi-Agent Pipeline | ✅ Complete | Dual-label tasks → pipeline:research_code DAG; pipeline-worker deployed |
| 9 | Observability + Prompts | ✅ Complete | Langfuse deployed; @observe() on all tools; eval scores wired; coder-system v4 prompt live; real Hatchet run traced with tool call spans (trace 7c8cb711) |
| 10 | MCP Gateway | ✅ Complete | mcp-searxng + LiteLLM gateway live; agents wired; 2 tools verified at /mcp endpoint |
| 11 | Scaffold Worker | ⬜ Not started | Requires Phase 14 (chat dispatch) as pre-condition |
| 12 | Control Plane UI | ⏭ Skipped | Marked manual — do not implement |
| 13 | Infra Stabilization | ✅ Complete | 13a: mem0 → RPi; 13b: qa+reviewer images built; 13c: prometheus on murderbot; 13d: PR #747 merged |
| 14 | Conversational Dispatch | ⬜ Not started | claw.amer.dev → Praetor; /chat/dispatch endpoint + OpenWebUI Tool; pre-condition for Phase 11 |

## OpenWebUI (claw.amer.dev)

Admin credentials reset on 2026-06-13. Login with:
- **URL:** https://claw.amer.dev
- **Email:** `amerenda@proton.me`
- **Password:** stored in BWS as `openwebui-dean-admin-password`

---

## Phase 7 — Webhook Setup (Completed)

The `amerenda-reviewer` GitHub App is installed on all repos in the org and delivers
`pull_request` events to `https://pubhooks.amer.dev/praetor/webhooks/github` (public
ingress via Traefik, strips `/praetor` prefix, routes to praetor-webhook-adapter).

No org-level webhook needed. No manual curl. Verify end-to-end by opening a test PR.
