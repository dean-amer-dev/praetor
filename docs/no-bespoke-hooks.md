# No Bespoke Tool-Calling Hooks

## Rule

**Never add custom LiteLLM proxy hooks to work around tool-calling behaviour.**

The `tool_strip_hook` (formerly at `k3s-dean-gitops/apps/litellm/server/tool-strip-hook-configmap.yaml`) was removed because it was a bespoke proxy that patched around problems that should be fixed at the source.

## What was wrong

The hook grew to include:
- Synthesis signal injection (injecting "stop calling tools" text into tool results)
- Loop detection (forcing synthesis after N tool calls)
- Thinking-budget stripping (disabling thinking mid-loop to avoid max_tokens exhaustion)
- Context budget stripping (dropping old turns when context grew too large)

None of these are community-backed patterns. They are symptoms of misconfigured models.

## How to fix tool-calling problems correctly

### Synthesis (model keeps calling tools and never writes an answer)
Fix in the **system prompt**: `"After 5 tool calls, STOP calling tools and write your answer."`
Do NOT inject signals via a proxy.

### Reasoning-budget exhaustion (empty content after thinking)
Root cause: `max_tokens` in llama.cpp is a total budget (thinking + output). When thinking
uses all of `max_tokens`, content is empty.

Fix in the **model config**: disable thinking via `chat_template_kwargs: {enable_thinking: false}`.
The 27B dense model does not benefit from thinking in interactive research sessions — disable it.
The 35B MoE with thinking works at `max_tokens >= 2048`.

Do NOT add a hook stage to strip thinking mid-request.

### Orphaned tool results (400 Bad Request from malformed history)
This is a real edge case. LiteLLM handles it natively. If you need to test it,
write a test that exercises the actual API boundary — do not add a hook.

### Context overflow (conversation too long)
The model will return a context-length error. Handle this in the client by starting
a new conversation or summarizing. Do NOT silently drop old turns via a hook.

## Enforcement

- The `tool_strip_hook.py` configmap is a stub (one comment line)
- The `callbacks` list in LiteLLM config only contains `prometheus`
- CI smoke tests call the real model to catch regressions — no mock hooks

When a tool-calling regression appears, add a **test** that reproduces it, then fix
the **model config** or **system prompt**. Do not add hook code.
