# Obsidian Vault Conventions

Canonical reference for how the `Dean` Obsidian vault is structured. This
file is the source of truth (version-controlled); a mirrored copy lives at
`Documentation/obsidian-vault-conventions.md` in the vault for browsing
inside Obsidian. If they ever diverge, this repo copy wins — re-sync the
vault copy from here.

Access from an agent: the `obsidian-mcp` server (`read_note`, `write_note`,
`edit_note`, `list_notes`, `list_folders`, `list_tags`, `delete_note`,
`move_note`, `get_note_metadata`). No filesystem mirror exists — the vault
is reached only over the CouchDB LiveSync protocol via that MCP server.

## Structure (PARA)

```
Areas/                 # standing responsibilities, no end state
  <Name>/
    _status.md
    notes/
    Plans/
Projects/               # bounded initiatives with a finish line
  <Name>/
    _status.md
    notes/
    Plans/
Documentation/           # durable "how a live system works" docs
Incidents/               # postmortems, YYYY-MM-DD-slug.md
Life/                    # personal, non-project
Archive/                 # completed/dead work, moved here wholesale
Prompts/
Templates/               # this repo's docs/templates/*.md, mirrored
Todo.md                  # single flat todo list, all Areas/Projects
```

**Area vs. Project:** an Area has no finish line (infra upkeep, a device
category, a hosted service). A Project will eventually be done or
archived. Ask which kind before creating a new folder — don't default to
Projects for everything.

**Folder shape:** every Area/Project gets exactly `_status.md`, `notes/`,
`Plans/` — created implicitly on first write, no scaffolding step needed.

## Casing (do not drift)

- Plans subfolder: always `Plans/` (capital P)
- Notes subfolder: always `notes/` (lowercase n)
- Never rename between them with `move_note` — the CouchDB backing store
  lowercases doc IDs, so a case-only rename collides both paths onto one
  document and can silently delete content. To recase: `read_note` →
  `delete_note` → `write_note` under the new path.

## Frontmatter

Every new or edited note gets minimal plain-YAML frontmatter, no plugin
required:

```yaml
---
status: active | stale | expired
type: plan | note | status | incident
---
```

- `status` is a judgment call: `stale` = probably outdated, not confirmed
  dead; `expired` = confirmed no longer relevant (candidate for
  `Archive/`, but expired status alone doesn't move the file).
- `type` classifies the note for future sweeps — no Dataview plugin,
  sweeps are agent-driven by reading frontmatter directly.

## The four note types

| Type | Template | Where | Purpose |
|------|----------|-------|---------|
| Status | `templates/status.md` | `<Area\|Project>/_status.md` (one per folder) | Current state snapshot, always overwritten in place |
| Plan | `templates/plan.md` | `<Area\|Project>/Plans/<slug>.md` | Action checklist only — see rule below |
| Notes | `templates/notes.md` | `<Area\|Project>/notes/<slug>-background.md` (shared) or `<slug>-phaseN-notes.md` (per phase) | Freeform background/diagnosis, scoped per phase — see below |
| Incident | `templates/incident.md` | `Incidents/YYYY-MM-DD-<slug>.md` | Postmortem for a user-visible or infra-impacting event |

## Plans are action checklists, not history

A plan is what to do, in order, as checkboxes, with a checkable success
criterion per phase — nothing else. Background, diagnosis, root-cause
narrative, decisions-and-why, inventory tables all belong in a companion
`notes/` note, linked by path from the top of the plan, never inlined. If
a plan item grows a "because..." clause, that clause moves to the note.

**Notes are scoped per phase, not one growing file.** A single background
note (`<slug>-background.md`, linked from the top of the plan) holds only
root-cause context shared across the whole plan, written once before
Phase 1. Detail discovered while executing a specific phase goes in its
own `<slug>-phaseN-notes.md`, linked from that phase's section in the
plan — not appended to the background note or to a prior phase's note.
Once a phase's "Done when" is checked, its note is closed; later phases
get a fresh file. This keeps any single note bounded to one phase's worth
of content instead of accumulating the whole project's history, and lets
an agent load only the phase note relevant to the phase it's working on.

**Phase discipline:** phases are strictly sequential. Don't start phase
N+1 until phase N's "Done when" is fully met. A phase involving one or
more PRs is never done until **all** of those PRs are merged —
unconditionally, even if the phase's own "Done when" text doesn't mention
merge, and regardless of how many PRs the phase has.

## Plan execution: update on stop

A plan is a resumable unit of work, not a one-shot script — whoever picks
it up next (a cleared context, a different session) needs to read it cold
and know exactly where things stand. So whenever executing a plan, update
the plan file itself at every stopping point — a step or phase completes,
a blocker is hit, permission is needed, or the turn simply ends — before
yielding control back:

- Check off every step that's actually done (not "started").
- If a phase is left mid-way, leave a short note of what was done and
  what's next — in that phase's section of the plan, or its
  `notes/<slug>-phaseN-notes.md` — rather than leaving the next session to
  re-derive it from git history or memory.

This is how every phased plan in this vault is meant to be executed,
standing rule — not busywork added only when a plan happens to ask for it.

## Documentation graduation boundary

`notes/` holds work-in-progress research/status. Once a note matures into
a description of how a *live* system actually works, graduate it to
`Documentation/` (or the wiki at wiki.amer.dev for deep infra detail) —
don't leave a permanent duplicate in `notes/`.

## Todo.md tagging

Single flat list at vault root, not per-project files. Tag with
`#<project-slug>` when known (`#infrastructure`, `#archlinux`,
`#home-assistant`, ...) — untagged is fine, it's just unsorted.

Mode tags: `#ai-go`, `#ai-no-cutover`, `#ai-plan-only`, `#ai-research`,
`#blocked`, `#on-site-only`.
