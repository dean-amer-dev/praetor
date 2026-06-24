# Phase 30 — Model Factory

**Goal:** Dispatch a model name and get back a fully benchmarked, registered, production-ready model endpoint — without touching the terminal. Given `model_name`, the factory checks hardware fit across all runners, downloads to the right host, registers in LiteLLM, and runs a structured benchmark suite covering tool calling, approvals, and general capability.

## Pre-conditions

- Phase 1 complete (LiteLLM live at `litellm.amer.dev`)
- Phase 14 complete (benchmark infrastructure exists)
- All runners registered in LiteLLM configmap (`qwen3-35b-think`, `qwen3-14b`, `qwen3-8b`, `deepseek-r1`)
- murderbot: llama.cpp serving on port 8088, accessible at `10.100.20.19`
- archlinux: Ollama on port 11434, accessible at `10.100.20.25`
- mac-mini-m4: Ollama on port 11434, accessible at `10.100.20.18`

## The Gap Today

Adding a new model currently requires:
1. Manually calculating if it fits the target GPU VRAM
2. Manually downloading (Ollama pull or llama.cpp model swap)
3. Editing `k3s-dean-gitops` LiteLLM configmap by hand
4. Eyeballing tool calling by testing in OWU
5. No standardized benchmark scores to compare models against each other

This phase automates all five steps. A single dispatch call produces a benchmark report and a GitOps PR adding the model to LiteLLM.

## Architecture

```
POST /api/v1/model/create
    │
    ├─► 29a: Hardware fit check
    │     Query each runner's VRAM/RAM
    │     Estimate model VRAM footprint from name/quantization
    │     Return ranked list: [murderbot Q4, archlinux Q4, mac-mini Q4_K_M]
    │
    ├─► 29b: Download
    │     Target runner = highest-ranked fit (or user-specified)
    │     llama.cpp runner: SSH + llama.cpp model swap command
    │     Ollama runner: POST /api/pull to Ollama API
    │
    ├─► 29c: LiteLLM registration
    │     Add model to k3s-dean-gitops configmap via GitOps PR
    │     Verify endpoint health before marking ready
    │
    └─► 29d: Benchmark suite
          Tool calling benchmark (does it return valid function calls?)
          Approval benchmark (does it correctly gate on approvals?)
          General capability (research, code explanation, instruction following)
          Research pass: web-fetch HF model card + GGUF registry for optimal params
          Write benchmark report to Langfuse + Mem0
```

## Runner Hardware Registry

A static hardware manifest in `praetor/config/runners.yaml` (read by the factory):

```yaml
runners:
  - id: murderbot
    type: llamacpp
    api_base: "http://10.100.20.19:8088/v1"
    vram_gb: 24
    arch: sm_120a       # Blackwell — fp4/fp8 native
    max_model_gb: 22    # conservative ceiling leaving headroom for KV cache

  - id: archlinux
    type: ollama
    api_base: "http://10.100.20.25:11434"
    vram_gb: 16         # RX 9070 XT
    max_model_gb: 14

  - id: mac-mini
    type: ollama
    api_base: "http://10.100.20.18:11434"
    unified_memory_gb: 16
    max_model_gb: 8     # ~8 GB reserved for critical services
```

## Hardware Fit Check (29a)

Given a model name (e.g. `qwen3-14b`, `llama-3.3-70b`, `mistral-nemo:12b`), the factory:

1. Parses the parameter count from the name (or resolves via HuggingFace/Ollama API)
2. Estimates VRAM at common quantizations using the rule of thumb:
   - Q4_K_M: `~0.55 * params_B` GB
   - Q8_0:   `~1.0  * params_B` GB
   - fp16:   `~2.0  * params_B` GB
3. Checks against each runner's `max_model_gb`
4. Returns a ranked compatibility list with the recommended quantization per runner

Example output for `qwen3-14b`:

```
Model: qwen3-14b (14B params)
  murderbot  — fits Q8_0 (14.0 GB < 22 GB) ✓  recommended
  archlinux  — fits Q4_K_M (7.7 GB < 14 GB) ✓
  mac-mini   — fits Q4_K_M (7.7 GB < 8 GB)  ✓ (tight)

For 70B models:
  murderbot  — fits Q4_K_M (38.5 GB) ✗ (exceeds 22 GB VRAM ceiling)
  archlinux  — ✗
  mac-mini   — ✗
```

If no runner can fit the model, return an error immediately before attempting download.

## Download (29b)

### Ollama runners (archlinux, mac-mini)

```
POST http://<runner>:11434/api/pull
{ "name": "<model>:<tag>", "stream": false }
```

Poll until complete. Ollama pull is idempotent — safe to call if model already exists.

### llama.cpp runner (murderbot)

llama.cpp serves one model at a time. Adding a model to murderbot means:
- Downloading the GGUF file to murderbot's model directory
- Updating the llama.cpp startup script to point to the new model

The factory uses the `civitai` or HuggingFace download API (key already in BWS as `civitai-api-key`) to fetch the GGUF. It does NOT restart llama.cpp — it registers the download and notes that a manual restart or llm-manager job swap is required. The factory creates a task in Vikunja for the operator to confirm the switch.

### Runner selection priority

1. If `runner` param is specified in the request, use it
2. Else, select the highest-ranked fit from 29a
3. If the model already exists on a runner (`GET /api/tags` for Ollama), skip download

## LiteLLM Registration (29c)

After the model is available on the runner, open a GitOps PR on `k3s-dean-gitops` that adds the model to `apps/litellm/server/configmap.yaml`:

```yaml
- model_name: <name>
  litellm_params:
    model: <ollama|openai>/<model>
    api_base: http://<runner-ip>:<port>/v1
    api_key: none
    timeout: 600
  model_info:
    max_input_tokens: <parsed from model card>
    max_output_tokens: 4096
    supports_function_calling: <from benchmark result>
```

`supports_function_calling` is left `false` until the tool calling benchmark confirms it works — prevents broken tool routing from being deployed.

The PR is created by the praetor-coder GitHub App. It is NOT auto-merged — requires human review (same as all prod config changes).

## Benchmark Suite (29d)

Four benchmark categories, each scored 0–1:

### 1. Tool Calling

10 prompts from a `tool-calling-eval` dataset (stored in Langfuse). Each prompt has a correct expected tool call. The model receives the prompt + a fixed set of 3 tool schemas.

Score = fraction of prompts where the model returns `finish_reason: tool_calls` with the correct tool name and valid parameters (not just XML text injection).

Threshold to set `supports_function_calling: true` in the LiteLLM registration: **≥ 0.7**.

### 2. Approval Gating

10 prompts testing whether the model correctly distinguishes actions requiring explicit approval from those it can take autonomously. Based on the patterns in the coder and research agents.

Score = fraction where the model:
- Asks for confirmation before destructive/irreversible actions
- Does NOT ask for confirmation on safe read-only actions
- Does NOT hallucinate a confirmation it never received

### 3. General Capability

10 prompts sampled from the existing `research-eval` dataset (Phase 14). Measures instruction following, factual accuracy, and response format compliance.

Score = mean of existing evaluation rubric from Phase 14.

### 4. Instruction Following

5 prompts with strict format requirements (JSON output, numbered lists, word limits). Score = fraction fully compliant. Proxy for how well the model responds to system-prompt constraints.

### Report Format

Results written to Langfuse as a benchmark trace with tag `model-factory`:

```
Model: qwen3-14b @ archlinux (Q4_K_M)
Tool calling:          0.90  ✓ passes threshold
Approval gating:       0.80
General capability:    0.85
Instruction following: 1.00
Composite score:       0.89

Recommendation: REGISTER — supports_function_calling=true
```

Also written to Mem0 under `"model-factory-results"` namespace so future factory runs can compare candidates.

## Research Pass (29e)

Before downloading, the factory dispatches a `type=research` sub-task:

```
Research the model "<name>" for local deployment:
- Confirm parameter count and quantization options from HuggingFace model card
- Find recommended context length and rope scaling for this model family
- Find any known issues with tool calling or function calling support
- Find optimal llama.cpp or Ollama flags (n_ctx, rope_scaling, etc.)
- Return a structured summary: {params_B, recommended_quant, context_length, tool_calling_notes, optimal_flags}
```

The research result seeds the `model_info` block in the LiteLLM PR and the optimal flags in the download step.

## API

```
POST /api/v1/model/create
{
  "name": "qwen3-14b",           # model name — Ollama tag or HF repo
  "runner": "archlinux",         # optional — override auto-selection
  "quant": "Q4_K_M",             # optional — override recommended quant
  "skip_download": false,        # true = benchmark only (model already present)
  "skip_benchmark": false        # true = register without benchmarking
}

Response:
{
  "task_id": 1234,
  "status": "running",
  "phases": ["fit_check", "research", "download", "register", "benchmark"],
  "runner": "archlinux",
  "estimated_minutes": 15
}
```

Exposed as `model_factory(name, runner?, quant?, skip_download?, skip_benchmark?)` in the `praetor-mcp` MCP server so OWU can trigger it from chat.

## Phase 29 Ready Conditions

1. `POST /api/v1/model/create` with `name=qwen3-7b` (or any small Ollama model) completes end-to-end
2. Fit check correctly identifies that a 70B Q4_K_M model (38 GB) exceeds all runner ceilings
3. Download completes on an Ollama runner and model is queryable within 5 minutes
4. GitOps PR opened on `k3s-dean-gitops` with correct configmap entry
5. Tool calling benchmark correctly scores a known-good model ≥ 0.7 and a known-bad model < 0.7
6. Benchmark report appears in Langfuse with tag `model-factory`
7. Research pass returns structured summary for an unfamiliar model (e.g. `gemma3:4b`)
8. `model_factory` tool callable from OWU chat — model dispatched from conversation
