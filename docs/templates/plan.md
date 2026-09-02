---
status: active
type: plan
---

<!--
This plan is a concise action checklist — what to do, in order, with a
checkable success criterion per phase. Nothing else. No narrative about
how a decision was reached, no rationale for settled decisions, no
scene-setting. Background/diagnosis goes in the companion notes/ file
linked below, never inlined here.
-->

Background: `<Area|Project>/notes/<slug>-background.md`
(read first if one exists — root-cause narrative shared across phases,
source files, open decisions. Keep this file scoped to what's true for
the whole plan, not per-phase detail — see each phase's own Notes link
below for that.)

Phases are strictly sequential — do not start phase N+1 until phase N's
"Done when" is fully checked, including live verification if it calls for
one, AND its PR is merged (a PR-involving phase is never done until
merged, unconditionally).

## Phase 1 — <name>

Notes: `<Area|Project>/notes/<slug>-phase1-notes.md` (only if this phase's
diagnosis/detail needs more than a line or two here — see notes.md
template. Skip entirely for a phase simple enough not to need one.)

- [ ] <step>
- [ ] <step>

- [ ] **Done when:** <objectively checkable criterion — endpoint returns
      200, pods Running with no CrashLoopBackOff, feature tested live,
      etc. Leave unchecked until fully verified, PR merge included.>

## Phase 2 — <name>

Notes: `<Area|Project>/notes/<slug>-phase2-notes.md` (own file, not
appended to phase 1's — a closed phase's note doesn't grow further once
its "Done when" is checked.)

- [ ] <step>

- [ ] **Done when:** <criterion>
