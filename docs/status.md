# Praetor Platform — Phase Status

| Phase | Name | Status | Notes |
|-------|------|--------|-------|
| 0 | Deployment Foundation | ✅ Complete | infra-mcp, bws-mcp, GitHub apps deployed |
| 1 | Inference | ✅ Complete | LiteLLM at litellm.amer.dev |
| 2 | Storage | ✅ Complete | Qdrant on Mac Mini via Komodo |
| 3 | Dispatch | ✅ Complete | Hatchet Lite on k3s, stub worker |
| 4 | Memory | ✅ Complete | Mem0 at mem0.amer.dev |
| 5 | Research Agent | ✅ Complete | Vikunja ai-research label → research worker |
| 6 | Coder Agent | ✅ Complete | Vikunja ai-go label → coder worker, PRs via dean-coder[bot] |
| 7 | PR Reviewer + QA | 🟡 Partial | Code deployed; GitHub org webhook requires manual registration (see below) |
| 8 | Multi-Agent Pipeline | ✅ Complete | Dual-label tasks → pipeline:research_code DAG; pipeline-worker deployed |
| 9 | Observability + Prompts | ⬜ Not started | |
| 10 | MCP Gateway | ⬜ Not started | |
| 11 | Scaffold Worker | ⬜ Not started | |
| 12 | Control Plane UI | ⏭ Skipped | Marked manual — do not implement |

## Phase 7 Blocker — GitHub Org Webhook

The reviewer worker and QA worker are deployed. The webhook handler at
`https://praetor.amer.dev/webhooks/github` is live. One manual step remains:

**Register the GitHub org webhook** so GitHub delivers `pull_request` events to praetor.

```bash
# Run this with a PAT that has admin:org_hook scope (or use the dean-reviewer app token)
curl -X POST \
  -H "Authorization: Bearer <PAT_WITH_ADMIN_ORG_HOOK>" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/orgs/amerenda/hooks \
  -d '{
    "name": "web",
    "active": true,
    "events": ["pull_request"],
    "config": {
      "url": "https://praetor.amer.dev/webhooks/github",
      "content_type": "json",
      "secret": "acaad594f040cd36a2dc900142efee6431182dc85319c748893483b033c0a964",
      "insecure_ssl": "0"
    }
  }'
```

Once registered, open any PR on `amerenda/*` to verify Hatchet receives `github:pr_opened` and `dean-reviewer[bot]` posts a review comment.
