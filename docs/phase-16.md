# Phase 16 — Voice Dispatch

**Goal:** Speak a command to Home Assistant → Praetor agent runs → HA announces the result via TTS. Voice is a first-class dispatch interface, identical in capability to claw.amer.dev or opencode.

## Pre-conditions

- Phase 15 complete (MCP factory stable — platform fully instrumented)
- Phase 14 complete (quality baselines established — voice must hit known-good agents)
- `POST /api/v1/dispatch` live (Phase 11)
- `GET /api/v1/status/{task_id}` live (status polling endpoint)
- Piper TTS running (already in `llm-agents` stack on mac-mini-m4)
- Home Assistant at `https://ha.amer.dev` with `$HA_TOKEN` available

## Architecture

```
"Hey, research Tailscale exit nodes"
          │
          ▼
  Home Assistant
  (voice command intent: praetor_dispatch)
          │
          ▼
  HA → REST API → POST /api/v1/dispatch
          │
          ▼
  Hatchet dispatches agent
          │
          ▼
  HA polls GET /api/v1/status/{task_id} (every 30s, up to 20 min)
          │
          ▼
  done=true → HA calls piper TTS with mem0_summary
          │
          ▼
  HA announces result via media player
```

No new infrastructure. Voice uses the same dispatch API as every other interface.

## What Gets Built

### 16a — HA Intent Script (`praetor_dispatch`)

A Home Assistant script that calls the Praetor dispatch API and polls for results.

Add to `ha.amer.dev` config (via `docker exec homeassistant` → `/config/scripts.yaml` or UI):

```yaml
praetor_dispatch:
  alias: "Dispatch Praetor Agent"
  description: "Send a task to the Praetor platform and announce the result."
  fields:
    task_title:
      description: "The task to run"
      example: "Research Tailscale exit nodes"
    task_type:
      description: "research | code | pipeline"
      default: "research"
  sequence:
    - service: rest_command.praetor_dispatch
      data:
        title: "{{ task_title }}"
        type: "{{ task_type }}"
      response_variable: dispatch_response

    - variables:
        task_id: "{{ dispatch_response.content | from_json | attr('task_id') }}"

    - service: tts.speak
      data:
        message: "Got it. Running {{ task_type }} task. I'll let you know when it's done."
        media_player_entity_id: media_player.living_room

    - repeat:
        count: 40  # max 20 minutes (30s × 40)
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
                    Praetor result: {{ (status_response.content | from_json).tts_summary }}
                  media_player_entity_id: media_player.living_room
              - stop: "Task complete"
```

### 16b — HA REST Commands

Add to `/config/configuration.yaml`:

```yaml
rest_command:
  praetor_dispatch:
    url: "https://praetor.amer.dev/api/v1/dispatch"
    method: POST
    headers:
      Authorization: "Bearer {{ states('input_text.praetor_api_key') }}"
      Content-Type: application/json
    payload: '{"title": "{{ title }}", "type": "{{ type }}"}'

  praetor_status:
    url: "https://praetor.amer.dev/api/v1/status/{{ task_id }}"
    method: GET
    headers:
      Authorization: "Bearer {{ states('input_text.praetor_api_key') }}"
```

`input_text.praetor_api_key` — a helper that stores the `PRAETOR_API_KEY` value. Set it once in the HA UI; never hardcode in YAML.

### 16c — Voice Intent Registration

Register a custom intent in HA's conversation integration so the local voice assistant parses "research X" and "code X in repo Y" into structured calls.

```yaml
# /config/custom_sentences/en/praetor.yaml
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
          - "implement {task} in repo {repo}"
          - "write code for {task}"
```

Intent handlers in `/config/intent_script.yaml`:

```yaml
PraetorResearch:
  action:
    service: script.praetor_dispatch
    data:
      task_title: "Research: {{ topic }}"
      task_type: research
  speech:
    text: "Starting research on {{ topic }}."

PraetorCode:
  action:
    service: script.praetor_dispatch
    data:
      task_title: "{{ task }}"
      task_type: code
  speech:
    text: "Got it. I'll start coding {{ task }}."
```

### 16d — TTS Response Length Handling

Agent research results can be long. The TTS output must be trimmed to something speakable.

Add `tts_summary` field to the `/api/v1/status` response:

```python
# In webhooks/dispatch_api.py status endpoint
mem0_summary = memories[0]["memory"] if memories else None
tts_summary = None
if mem0_summary:
    # First 2 sentences only for TTS — full summary still in mem0_summary
    sentences = mem0_summary.split(". ")
    tts_summary = ". ".join(sentences[:2]) + "."

return {
    "task_id": task_id,
    "done": bool(mem0_summary),
    "mem0_summary": mem0_summary,
    "tts_summary": tts_summary,
}
```

HA reads `tts_summary` instead of `mem0_summary` for the spoken result.

## Phase 17 Ready Conditions

1. "Hey assistant, research Tailscale exit nodes" → `agent:research` Hatchet run starts within 15s
2. HA announces "Got it. Running research task." immediately after dispatch
3. HA polls status and announces the TTS summary when `done=true` (within 20 minutes)
4. `PraetorCode` intent: "code add /healthz to ecdysis" → coder agent starts, HA confirms
5. `input_text.praetor_api_key` helper holds the key — no hardcoded secrets in HA YAML
6. TTS summary is ≤3 sentences (not raw dump of full research output)
7. Works from both living room and bedroom media players
