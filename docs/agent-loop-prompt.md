# Praetor Agent Loop Prompt

Paste this prompt to continue implementing the next unfinished phase.

---

Read `/home/alex/claude/projects/praetor/docs/status.md` and `/home/alex/claude/projects/praetor/docs/phase-N.md` for the next incomplete phase, then implement it.

**Rules:**
- Do not start Phase 12 (Control Plane UI) — it is marked manual and must be skipped
- Before writing any code, verify both repos are on clean main branches (`git status`)
- Follow the phase doc exactly — do not add features beyond what it specifies
- When implementation is done, run every ready condition listed in the phase doc and confirm each one passes before marking complete
- Update `docs/status.md` to mark the phase complete (or blocked with the specific blocker) and commit it in the same PR as the implementation
- Open PRs — never push directly to main
- Stop after completing one phase — do not chain into the next

**Working directories:**
- Praetor repo: `/home/alex/claude/projects/praetor/`
- GitOps repo: `/home/alex/claude/projects/k3s-dean-gitops/`
- Komodo GitOps: `/home/alex/claude/projects/komodo-dean-gitops/`

**If a phase has blockers** (e.g., requires a human to generate an API token or register a GitHub App), implement everything that can be automated, open the PR, then update status.md with the specific manual step needed and stop.
