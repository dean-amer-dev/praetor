# Phase 29 — Model Factory

## You are implementing Phase 29 of the Praetor platform.

**You are allowed to merge PRs for this session.**

---

## Context

You are working across `amerenda/praetor` (factory API + benchmark agent), `amerenda/k3s-dean-gitops` (k3s GitOps), and `amerenda/dean-mcp` (praetor-mcp MCP server). The platform runs on a k3s cluster managed by ArgoCD. Secrets are in BWS exclusively.

### What already exists

- **Phase 14** — `agents/model_benchmark/` — benchmark agent and runner; eval datasets in Langfuse; `POST /api/v1/benchmark/run`
- **Phase 1** — LiteLLM at `litellm.amer.dev` routing models to runners; configmap at `apps/litellm/server/configmap.yaml` in `k3s-dean-gitops`
- `lm_praetor_dispatch` in praetor-mcp — OWU can trigger dispatches from chat
- Runners:
  - **murderbot** — llama.cpp on port 8088, `http://10.100.20.19:8088/v1`, 24 GB VRAM (sm_120a Blackwell)
  - **archlinux** — Ollama on port 11434, `http://10.100.20.25:11434`, 16 GB VRAM (RX 9070 XT)
  - **mac-mini** — Ollama on port 11434, `http://10.100.20.18:11434`, 16 GB unified (8 GB reserved for critical services)
- Civitai API key in BWS as `civitai-api-key` (for GGUF downloads)

### What is missing

Adding a model today requires: manual VRAM math, manual download, hand-editing the LiteLLM configmap, and eyeballing tool calling in OWU. There is no standardized benchmark score to compare candidates. Phase 29 automates all five steps: fit check → research → download → LiteLLM registration → benchmark.

---

## Your constraints

**GitOps only.** LiteLLM configuration changes go through a PR on `k3s-dean-gitops`. The model factory opens this PR automatically — that is its job. Merging the LiteLLM PR requires human approval (production config change). No direct configmap edits via kubectl.

**BWS is the single source of truth for all secrets.** Civitai API key, Ollama API keys (if needed), and any runner credentials must come from BWS. No secrets in Git.

**No server restarts.** The factory downloads models and opens GitOps PRs. For llama.cpp on murderbot, switching to a new model requires a server restart (which Alex does manually). The factory must create a Vikunja task for the operator to confirm the switch — it does NOT initiate the restart itself. For Ollama runners, `POST /api/pull` is idempotent and does not require a restart.

**Ansible-playbooks for infrastructure only.** No new host-level dependencies needed for this phase.

---

## What to build

### 1. `praetor/config/runners.yaml`

Static hardware manifest, read by the factory at startup:

```yaml
runners:
  - id: murderbot
    type: llamacpp
    api_base: "http://10.100.20.19:8088/v1"
    vram_gb: 24
    arch: sm_120a
    max_model_gb: 22

  - id: archlinux
    type: ollama
    api_base: "http://10.100.20.25:11434"
    vram_gb: 16
    max_model_gb: 14

  - id: mac-mini
    type: ollama
    api_base: "http://10.100.20.18:11434"
    unified_memory_gb: 16
    max_model_gb: 8
```

### 2. `POST /api/v1/model/create` in `webhooks/model_factory.py`

```python
class ModelCreateRequest(BaseModel):
    name: str                    # e.g. "qwen3-14b", "llama-3.3-70b"
    runner: str | None = None    # override auto-selection
    quant: str | None = None     # override recommended quantization
    skip_download: bool = False  # benchmark only (model already present)
    skip_benchmark: bool = False # register without benchmarking
```

Returns `{ task_id, status, phases, runner, estimated_minutes }` and runs async via a Hatchet worker.

**Pipeline steps:**

#### 29a — Hardware fit check

Parse parameter count from the model name (or query HuggingFace API). Estimate VRAM at common quants:
- Q4_K_M: `~0.55 * params_B` GB
- Q8_0: `~1.0 * params_B` GB
- fp16: `~2.0 * params_B` GB

Check against each runner's `max_model_gb`. Return ranked compatibility list with recommended quant per runner. If no runner can fit the model at any quant, return an error immediately — do not proceed.

#### 29b — Research pass (dispatched as `type=research` sub-task)

Dispatch a research agent task before downloading:

```
Research the model "{name}" for local deployment:
- Confirm parameter count and quantization options from HuggingFace
- Find recommended context length and any known rope scaling requirements
- Find known issues with tool calling / function calling support
- Find optimal llama.cpp or Ollama flags (n_ctx, rope_scaling, etc.)
Return structured: {params_B, recommended_quant, context_length, tool_calling_notes, optimal_flags}
```

The research result seeds the LiteLLM `model_info` block and download flags.

#### 29c — Download

**Ollama runners** (archlinux, mac-mini):
```
POST http://{runner}:11434/api/pull
{ "name": "{model}:{tag}", "stream": false }
```
Poll until complete. If the model already exists (`GET /api/tags`), skip.

**llama.cpp runner (murderbot):**
Download the GGUF file to murderbot's model directory using the HuggingFace download API or Civitai API (key from BWS). Do NOT restart llama.cpp. Create a Vikunja task (`POST https://todo.amer.dev/api/v1/projects/18/tasks`) with title `"Switch llama.cpp to {name}"` and instructions for the operator. Mark the factory task as `awaiting_operator` until the Vikunja task is resolved.

#### 29d — LiteLLM registration

After the model is available on the runner, open a PR on `k3s-dean-gitops` patching `apps/litellm/server/configmap.yaml` to add:

```yaml
- model_name: {name}
  litellm_params:
    model: {ollama|openai}/{model}
    api_base: http://{runner-ip}:{port}/v1
    api_key: none
    timeout: 600
  model_info:
    max_input_tokens: {from research}
    max_output_tokens: 4096
    supports_function_calling: false   # updated to true after benchmark if score >= 0.7
```

Use the same `_upsert_litellm_config` / `_get_or_create_pr` pattern already in `webhooks/mcp_factory.py`. This PR is NOT auto-merged — it requires human review.

#### 29e — Benchmark suite

Run after the LiteLLM PR is merged and the model is queryable. Use the existing Phase 14 benchmark infrastructure (`agents/model_benchmark/bench.py`). Four categories scored 0–1:

1. **Tool calling** — 10 prompts from `tool-calling-eval` Langfuse dataset. Score = fraction returning valid `tool_calls` with correct name and params. **Threshold for `supports_function_calling: true`**: ≥ 0.7.
2. **Approval gating** — 10 prompts testing destructive-vs-safe action discrimination. Score = fraction of correct gate decisions.
3. **General capability** — 10 prompts from existing `research-eval` dataset (Phase 14).
4. **Instruction following** — 5 prompts with strict format requirements (JSON, numbered lists, word limits).

Results written to Langfuse as a benchmark trace tagged `model-factory`. Written to Mem0 under `"model-factory-results"` namespace for cross-model comparison.

If tool calling score ≥ 0.7, open a follow-up PR on `k3s-dean-gitops` updating `supports_function_calling: true` in the same configmap entry.

### 3. `GET /api/v1/model/status/{task_id}` in `webhooks/model_factory.py`

Returns current pipeline phase, runner, download progress, and benchmark scores as they accumulate.

### 4. `model_factory` tool in `dean-mcp/praetor-mcp/server.py`

```python
def model_factory(name: str, runner: str | None = None, quant: str | None = None,
                  skip_download: bool = False, skip_benchmark: bool = False) -> dict
```

Calls `POST /api/v1/model/create`. Makes the full pipeline triggerable from an OWU conversation.

### 5. Model benchmark worker deployment

The `model-benchmark` Hatchet worker (from Phase 14's `agents/model_benchmark/`) needs to be running as a k3s Deployment if it isn't already. Verify it exists; if not, add it via `k3s-dean-gitops` the same way other praetor workers are deployed.

### 6. Unit tests

Add `tests/unit/test_model_factory.py`:
- Fit check: 70B Q4_K_M (38 GB) exceeds all runner ceilings → error returned
- Fit check: 14B Q8_0 (14 GB) fits murderbot (< 22 GB) → murderbot recommended
- Ollama skip logic: model in existing tags → download step skipped
- LiteLLM configmap upsert: correct YAML structure generated

---

## Deployment

1. PR on `amerenda/praetor` — model_factory.py, runner config, model-benchmark worker if missing, CI matrix update → CI → deploy PR on `k3s-dean-gitops` → merge → ArgoCD syncs
2. PR on `amerenda/dean-mcp` — model_factory MCP tool → CI → deploy PR on `k3s-dean-gitops` → merge → ArgoCD syncs
3. The LiteLLM registration PRs opened by the factory itself require human merge (production config)

---

## Done when

1. `POST /api/v1/model/create` with `name="qwen3-7b"` (or any small Ollama model) runs end-to-end without error
2. Fit check correctly rejects a 70B model at all quants (38+ GB exceeds all runner ceilings)
3. Download completes on an Ollama runner and model is queryable at the runner's API within 5 minutes
4. GitOps PR opened on `k3s-dean-gitops` with the correct LiteLLM configmap entry
5. Benchmark report appears in Langfuse with tag `model-factory`; Mem0 has an entry under `model-factory-results`
6. `model_factory` tool callable from OWU — `model_factory("qwen3-7b")` triggers the pipeline from chat
7. All new k3s components (model-benchmark worker, praetor-mcp update) are `Running`
8. ArgoCD shows all affected applications (`praetor`, `praetor-mcp`) as `Healthy` and `Synced`
9. `komodo-dean-gitops` resource sync remains `Synced` and healthy (no regressions to stateful services)
