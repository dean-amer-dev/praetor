# OpenWebUI Tool Calling — Setup Requirements

## Overview

OpenWebUI supports two tool calling modes for models:

- **Text injection**: tool definitions are inserted into the system prompt; the model outputs XML; OpenWebUI parses and executes it.
- **Native function calling**: tools are passed as the OpenAI `tools` parameter; the model returns structured `tool_calls` JSON with `finish_reason: "tool_calls"`; OpenWebUI executes them via the MCP server.

OpenWebUI only triggers tool execution from `finish_reason: "tool_calls"`. Text injection output is treated as plain text and never executed. **Native function calling must be enabled explicitly on every model that uses tools.**

---

## LiteLLM MCP Tool Server

Configured at the admin level in OpenWebUI: Settings → Connections → Tool Servers.

| Field | Value |
|-------|-------|
| URL | `https://litellm.amer.dev/mcp/` |
| Type | `mcp` |
| Auth | Bearer — LiteLLM master key from BWS |

LiteLLM's MCP endpoint uses the **streamable HTTP MCP transport**. All requests must include both Accept headers:

```
Accept: application/json, text/event-stream
```

OpenWebUI sends these headers correctly when calling `/mcp/tools/list` and `/mcp/tools/call`.

---

## Model Requirements

For any model that has a Tool Server assigned (`toolIds`), the model's `params` must include:

```json
{ "function_calling": "native" }
```

Without this, OpenWebUI defaults to text injection and tool calls will never execute.

### Current models with tools enabled

| Model ID | Tool Server | function_calling |
|----------|-------------|-----------------|
| `qwen3-35b-think` | `server:mcp:litellm-mcp` | `native` |

### Setting via API

```bash
curl -X POST "https://bot.amer.dev/api/v1/models/model/update" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "<model-id>",
    "name": "<model-name>",
    "meta": { ... },
    "params": { "function_calling": "native" },
    "is_active": true,
    "access_grants": []
  }'
```

---

## LiteLLM Model Requirements

Each model used for tool calling must have `supports_function_calling: true` in its `model_info` block in LiteLLM's `configmap.yaml` (k3s-dean-gitops):

```yaml
model_info:
  supports_function_calling: true
```

This allows LiteLLM to pass the `tools` parameter through to the underlying backend.

---

## End-to-End Flow

1. OpenWebUI polls `POST /mcp/tools/list` periodically to refresh available tools from LiteLLM.
2. When a user sends a message, OpenWebUI includes tool definitions as native OpenAI `tools` in the chat completion request to LiteLLM.
3. LiteLLM passes `tools` to llama.cpp (using `--jinja` flag to apply the model's embedded chat template).
4. The model returns `finish_reason: "tool_calls"` with a structured `tool_calls` array.
5. OpenWebUI calls `POST /mcp/tools/call` on LiteLLM's MCP endpoint to execute the tool.
6. The result is fed back to the model as a `tool` role message.
7. The model generates the final response.

---

## Diagnostics

**Model outputs raw `<tool_call>` XML in content**: `function_calling` is not set to `"native"` on the model. Update via the API above.

**Tool calls are never attempted** (no `/mcp/tools/call` in LiteLLM logs): same cause — text injection mode is active.

**LiteLLM returns 406 "Not Acceptable"**: the MCP client is not sending `Accept: application/json, text/event-stream`. OpenWebUI sends these correctly; custom clients must add both headers.

**`session terminated` error for a tool server**: known issue with the mem0 MCP server — mem0 tools are excluded from available tools but do not block other MCP servers from working.
