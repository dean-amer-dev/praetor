# Phase 27 — Voice Dispatch

## You are implementing Phase 27 of the Praetor platform.

**You are allowed to merge PRs for this session.**

---

## Context

You are working across three repos: `amerenda/praetor` (webhook-adapter code), `amerenda/komodo-dean-gitops` (Home Assistant config), and `amerenda/k3s-dean-gitops` (k3s manifests). The platform runs on a k3s cluster (GitOps via ArgoCD). Home Assistant runs as a Docker container managed by Komodo on mac-mini-m4. Secrets come exclusively from Bitwarden Secrets Manager (BWS).

### What already exists

- `POST /api/v1/dispatch` — live, accepts `title`, `description`, `type`
- `GET /api/v1/status/{task_id}` — returns `{ task_id, done, mem0_summary }`
- Piper TTS — running in the `llm-agents` Komodo stack on mac-mini-m4
- Home Assistant — running at `https://ha.amer.dev`, managed by Komodo via `komodo-dean-gitops`
- Praetor API key in BWS as `praetor-api-key`

### What is missing

1. `GET /api/v1/status/{task_id}` does not return a `tts_summary` field (trimmed for speech)
2. Home Assistant has no scripts, rest_commands, or intent handlers for Praetor dispatch
3. There is no voice intent pipeline connecting HA's local voice assistant to the dispatch API

### Important: GitOps for Home Assistant config

Home Assistant config lives in a Docker volume on mac-mini-m4, managed by the Komodo `homeassistant` stack in `komodo-dean-gitops`. **All HA config changes must go through `komodo-dean-gitops` as versioned files — not via `docker exec` or direct file edits.** The HA container mounts a config directory from the host; that host path is tracked in the Komodo stack definition. Add new YAML files there. HA can hot-reload scripts and rest_command config without a container restart via `homeassistant.reload_scripts` and `homeassistant.reload_all` service calls.

---

## Your constraints

**GitOps only.** Every change must go through a PR:
- Code changes (tts_summary) → PR on `amerenda/praetor` → CI → deploy PR on `k3s-dean-gitops` → merge → ArgoCD syncs
- HA config changes → PR on `amerenda/komodo-dean-gitops` → Komodo syncs the stack → HA hot-reloads
- No `docker exec` to edit files. No manual file drops onto the host. No one-off commands.

**BWS is the single source of truth for all secrets.** The Praetor API key is in BWS as `praetor-api-key`. In HA, store it as an `input_text` helper set once via the HA UI (value sourced from BWS at setup time) — **never hardcode it in YAML files committed to git.**

**No container restarts.** HA supports hot-reload for scripts and rest_command. Use `homeassistant.reload_scripts` and `homeassistant.reload_all` to pick up config changes without restarting the container.

**Ansible-playbooks for infrastructure only.** This phase requires no host-level changes — no new packages, no new mounts. Pure application config.

---

## What to build

### 1. `tts_summary` field in `webhooks/dispatch_api.py`

Add to the `StatusResponse` model and `status()` endpoint:

```python
tts_summary: str | None = None
```

Population logic — first two sentences of `mem0_summary`, trimmed for speech:

```python
tts_summary = None
if mem0_summary:
    sentences = mem0_summary.split(". ")
    tts_summary = ". ".join(sentences[:2]).strip()
    if not tts_summary.endswith("."):
        tts_summary += "."
```

### 2. HA rest_command entries in `komodo-dean-gitops`

Find the Home Assistant stack configuration in `komodo-dean-gitops`. Add a `rest_command.yaml` (or extend the existing one) with:

```yaml
praetor_dispatch:
  url: "https://praetor.amer.dev/api/v1/dispatch"
  method: POST
  headers:
    Authorization: "Bearer {{ states('input_text.praetor_api_key') }}"
    Content-Type: application/json
  payload: '{"title": "{{ title }}", "description": "{{ description | default(title) }}", "type": "{{ type }}"}'

praetor_status:
  url: "https://praetor.amer.dev/api/v1/status/{{ task_id }}"
  method: GET
  headers:
    Authorization: "Bearer {{ states('input_text.praetor_api_key') }}"
```

### 3. HA dispatch script in `komodo-dean-gitops`

Add to `scripts.yaml` (or a new `praetor_scripts.yaml` include):

```yaml
praetor_dispatch_and_announce:
  alias: "Dispatch Praetor Agent"
  fields:
    task_title:
      description: "Task title"
    task_type:
      description: "research | code | pipeline | openhands"
      default: "research"
    task_description:
      description: "Optional detailed description"
  sequence:
    - service: rest_command.praetor_dispatch
      data:
        title: "{{ task_title }}"
        description: "{{ task_description | default(task_title) }}"
        type: "{{ task_type }}"
      response_variable: dispatch_response
    - variables:
        task_id: "{{ (dispatch_response.content | from_json).task_id }}"
    - service: tts.speak
      data:
        message: "Got it. Starting {{ task_type }} task."
        media_player_entity_id: media_player.living_room
    - repeat:
        count: 40
        sequence:
          - delay: "00:00:30"
          - service: rest_command.praetor_status
            data:
              task_id: "{{ task_id }}"
            response_variable: status_response
          - if:
              - condition: template
                value_template: >
                  {{ (status_response.content | from_json).done == true }}
            then:
              - service: tts.speak
                data:
                  message: >
                    {{ (status_response.content | from_json).tts_summary }}
                  media_player_entity_id: media_player.living_room
              - stop: "done"
```

### 4. HA voice intents in `komodo-dean-gitops`

Add `custom_sentences/en/praetor.yaml`:

```yaml
language: "en"
intents:
  PraetorResearch:
    data:
      - sentences:
          - "research {topic}"
          - "look up {topic}"
          - "find information about {topic}"
  PraetorCode:
    data:
      - sentences:
          - "code {task} in {repo}"
          - "implement {task} in {repo}"
```

Add `intent_script.yaml` entries:

```yaml
PraetorResearch:
  action:
    service: script.praetor_dispatch_and_announce
    data:
      task_title: "Research: {{ topic }}"
      task_type: research
  speech:
    text: "Starting research on {{ topic }}."

PraetorCode:
  action:
    service: script.praetor_dispatch_and_announce
    data:
      task_title: "{{ task }}"
      task_type: openhands
      task_description: "repo: {{ repo }}. {{ task }}"
  speech:
    text: "Got it. I'll start coding {{ task }}."
```

### 5. `input_text.praetor_api_key` helper

Add an `input_text` helper definition to the HA config (via `komodo-dean-gitops`). **The YAML defines the helper; the actual value is set once in the HA UI sourced from BWS — it is never written to a file.**

---

## Deployment

1. **Praetor code change** — PR on `amerenda/praetor` → CI → deploy PR on `k3s-dean-gitops` → merge → ArgoCD syncs
2. **HA config changes** — PR on `amerenda/komodo-dean-gitops` → Komodo syncs the `homeassistant` stack → HA hot-reloads scripts and rest_command (no restart needed)

After both deploy, verify the `input_text.praetor_api_key` helper value is set in the HA UI (one-time manual step, sourced from BWS `praetor-api-key`).

---

## Done when

1. `GET /api/v1/status/{task_id}` (on a completed task) returns a `tts_summary` field containing ≤ 2 sentences
2. Webhook-adapter pod in k3s is `Running` with the new image; ArgoCD shows `praetor` app `Healthy` and `Synced`
3. Komodo `homeassistant` stack is deployed and healthy; `komodo-dean-gitops` resource sync is `Synced` and healthy
4. HA intent "research Tailscale exit nodes" → `agent:research` Hatchet run starts within 15s
5. HA announces "Got it. Starting research task." immediately after dispatch
6. When the task completes, HA announces the TTS summary via the living room media player
7. `input_text.praetor_api_key` helper exists and holds a value — no API key is present in any committed YAML file
