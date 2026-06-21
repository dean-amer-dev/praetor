"""
Regression tests for qwen3-35b-think tool calling via LiteLLM.

Catches the class of bugs where multi-tool sessions fail with 400 Bad Request,
hang after N calls, or have reasoning-budget exhaustion during tool turns.

Run automatically in CI (SMOKE_TESTS=1 set by llm-regression job).
Run manually: SMOKE_TESTS=1 LITELLM_API_KEY=... pytest tests/smoke/test_llm_tool_calling.py -v
"""
import os
import subprocess
import uuid

import httpx
import pytest

pytestmark = pytest.mark.smoke

if not os.environ.get("SMOKE_TESTS"):
    pytest.skip("Set SMOKE_TESTS=1 to run LLM regression tests", allow_module_level=True)


# ── Config ────────────────────────────────────────────────────────────────────

def _k8s_secret(namespace: str, secret: str, key: str) -> str:
    try:
        out = subprocess.check_output(
            ["kubectl", "get", "secret", "-n", namespace, secret, "-o", f"jsonpath={{.data.{key}}}"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        import base64
        return base64.b64decode(out).decode().strip()
    except Exception:
        return ""


LITELLM_URL = os.environ.get("LITELLM_URL", "https://litellm.amer.dev")
MODEL       = os.environ.get("LLM_MODEL", "qwen3-35b-think")
TIMEOUT     = int(os.environ.get("LLM_TIMEOUT", "120"))

LITELLM_API_KEY = (
    os.environ.get("LITELLM_API_KEY")
    or _k8s_secret("litellm", "litellm-secrets", "master-key")
)


# ── Mock tool definitions ─────────────────────────────────────────────────────

MOCK_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web for information on a topic.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_url",
            "description": "Read and return the content of a URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "URL to read"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fact",
            "description": "Return a fact about a topic.",
            "parameters": {
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
            },
        },
    },
]

MOCK_TOOL_RESULTS = {
    "search_web": (
        "Search results: (1) Python is a high-level, general-purpose programming language created by Guido van Rossum "
        "in 1991. (2) Python emphasizes code readability and uses significant indentation. "
        "(3) Python 3 was released in 2008 and is the current major version. "
        "(4) Python is widely used in web development, data science, AI/ML, and scripting."
    ),
    "read_url": (
        "Article: Python's ecosystem includes NumPy, pandas, and scikit-learn for data science; "
        "TensorFlow and PyTorch for deep learning; Django and Flask for web development. "
        "Python consistently ranks as one of the top 3 most popular programming languages worldwide."
    ),
    "get_fact": (
        "Fact: Python was named after Monty Python's Flying Circus, not the snake. "
        "The Python Package Index (PyPI) hosts over 400,000 packages as of 2024."
    ),
}

# After this many tool calls, mock results inject a synthesis signal
_SYNTHESIS_SIGNAL_AFTER = 4
# After this many tool calls, stop offering tools so the model must synthesize
_FORCE_SYNTHESIS_AFTER  = 8


# ── Helpers ───────────────────────────────────────────────────────────────────

def _completion(messages, tools=None, max_tokens=2048):
    headers = {"Content-Type": "application/json"}
    if LITELLM_API_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_API_KEY}"
    payload = {"model": MODEL, "messages": messages, "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
    return httpx.post(
        f"{LITELLM_URL}/v1/chat/completions",
        json=payload,
        headers=headers,
        timeout=TIMEOUT,
    )


def _execute_mock_tool(name, args, call_number=0):
    result = MOCK_TOOL_RESULTS.get(name, f"Tool {name} returned: mock result for args {args}")
    if call_number >= _SYNTHESIS_SIGNAL_AFTER:
        result += (
            f"\n\n[You have now made {call_number + 1} tool calls and have sufficient information. "
            "Do NOT call any more tools. Write your final synthesized answer now.]"
        )
    return result


def _drive_tool_session(user_prompt, max_turns=15):
    """Drive a full tool-call session. Returns (turns, finish_reason, final_text, error)."""
    messages = [
        {
            "role": "system",
            "content": (
                "You are a helpful research assistant with access to search and reading tools. "
                "Use tools to gather information, then write a comprehensive answer. "
                "Once you have made 3-5 tool calls, stop calling tools and synthesize "
                "everything you have found into a final response. "
                "Do not make more than 8 tool calls total."
            ),
        },
        {"role": "user", "content": user_prompt},
    ]
    tool_call_count = 0

    for turn in range(max_turns):
        active_tools = MOCK_TOOLS if tool_call_count < _FORCE_SYNTHESIS_AFTER else None
        resp = _completion(messages, tools=active_tools)

        if resp.status_code != 200:
            return turn, "error", "", f"HTTP {resp.status_code}: {resp.text[:300]}"

        choice = resp.json()["choices"][0]
        msg    = choice["message"]
        finish = choice["finish_reason"]
        messages.append(msg)

        if finish in ("stop", "length"):
            # "length" = synthesis started but hit max_tokens — counts as synthesized
            return turn + 1, "stop", msg.get("content", ""), None

        if finish == "tool_calls":
            for tc in (msg.get("tool_calls") or []):
                fn     = tc["function"]["name"]
                args   = tc["function"].get("arguments", "{}")
                result = _execute_mock_tool(fn, args, call_number=tool_call_count)
                tool_call_count += 1
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc["id"],
                    "content":      result,
                })
            continue

        return turn + 1, finish, msg.get("content", ""), None

    return max_turns, "tool_calls", "", "hit max_turns without synthesis"


# ── Regression tests ──────────────────────────────────────────────────────────

class TestLLMToolCalling:
    def test_basic_tool_use(self):
        """Model uses 1-2 tools then synthesizes a real response."""
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")

        turns, finish, text, err = _drive_tool_session(
            "What is Python? Use the search tool to find information.",
            max_turns=10,
        )
        assert not err or "hit max_turns" in str(err), f"session error: {err}"
        assert finish == "stop", f"did not synthesize: finish={finish} after {turns} turns"
        assert text and len(text) >= 50, f"synthesis too short ({len(text)} chars): {text[:100]}"

    def test_extended_tool_use(self):
        """8+ tool calls must produce a synthesis — the core multi-tool regression."""
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")

        turns, finish, text, err = _drive_tool_session(
            (
                "Research the following and give me a comprehensive report: "
                "the history of the Python programming language, its major versions, "
                "its ecosystem, and its current usage in AI/ML. "
                "Search for each topic separately and read relevant URLs."
            ),
            max_turns=20,
        )
        assert not err or "hit max_turns" in str(err), f"session error: {err}"
        assert finish == "stop", (
            f"session did not synthesize after {turns} turns (finish={finish}). "
            "This is the multi-tool regression: model hung/stopped instead of writing final answer."
        )
        assert text and len(text) >= 100, f"synthesis too short ({len(text)} chars)"

    def test_complex_synthesis_non_empty(self):
        """
        Regression for reasoning-budget exhaustion on synthesis turns.

        Root cause: --reasoning-budget 1500 was too small for complex research
        prompts. When the model needs > 1500 thinking tokens, llama.cpp forces
        </think> and then stops generation, producing an empty response that
        OWU displays as a blank message.

        This test uses a complex multi-topic prompt (similar to real OWU usage),
        runs through 8 tool calls to gather context, then sends a DIRECT synthesis
        request (no tools, enable_thinking=true) and asserts the response is
        non-empty and substantive.

        Fails if reasoning-budget is too small (empty synthesis) or if budget
        exhaustion during tool turns causes 400 errors.
        """
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")

        # Build a realistic multi-tool session similar to a planning research prompt
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful technical assistant with access to search tools. "
                    "Use tools to gather information, then write a detailed technical plan."
                ),
            },
            {
                "role": "user",
                "content": (
                    "I want to deploy a Kubernetes MCP server with two access modes: "
                    "read-only and read-write. Research the best available image, "
                    "how others implement access control, and give me a detailed plan."
                ),
            },
        ]

        tool_call_count = 0
        for _ in range(_FORCE_SYNTHESIS_AFTER):
            resp = _completion(messages, tools=MOCK_TOOLS)
            assert resp.status_code == 200, f"tool turn HTTP {resp.status_code}: {resp.text[:200]}"
            choice = resp.json()["choices"][0]
            msg    = choice["message"]
            finish = choice["finish_reason"]
            messages.append(msg)

            if finish in ("stop", "length"):
                break
            if finish == "tool_calls":
                for tc in (msg.get("tool_calls") or []):
                    fn     = tc["function"]["name"]
                    args   = tc["function"].get("arguments", "{}")
                    result = _execute_mock_tool(fn, args, call_number=tool_call_count)
                    tool_call_count += 1
                    messages.append({
                        "role": "tool", "tool_call_id": tc["id"], "content": result,
                    })

        # Synthesis turn: no tools, thinking is ON (this is the path that was broken)
        resp = _completion(messages, tools=None, max_tokens=4096)
        assert resp.status_code == 200, f"synthesis HTTP {resp.status_code}: {resp.text[:300]}"

        choice  = resp.json()["choices"][0]
        content = choice["message"].get("content") or ""
        finish  = choice["finish_reason"]

        assert len(content) >= 200, (
            f"Synthesis response was empty or too short ({len(content)} chars, finish={finish}). "
            "This indicates reasoning-budget exhaustion on the synthesis turn: the model's "
            "<think> block was force-terminated and no content was generated after </think>. "
            "Check --reasoning-budget in llm/entrypoint.sh."
        )

    def test_no_thinking_with_tools(self):
        """
        Thinking must NOT activate during tool turns.
        If thinking fires, reasoning-budget exhaust truncates tool_call.arguments → 400
        on the next request. Verified indirectly: run 8 turns, assert no 400.
        """
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")

        turns, finish, text, err = _drive_tool_session(
            "Use the search tool to find 8 different facts about space exploration.",
            max_turns=20,
        )
        assert not ("400" in str(err)), (
            f"Got 400 — likely reasoning-budget exhaustion during tool turn. "
            f"Check auto_disable_thinking_with_tools in froggeric-v20.jinja. Error: {err}"
        )

    def test_orphan_cleanup(self):
        """Orphaned tool result (no matching assistant.tool_calls) → 200, not 400."""
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user",   "content": "Hello, what is 2+2?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_valid", "type": "function",
                                "function": {"name": "get_fact", "arguments": '{"topic": "math"}'}}],
            },
            {"role": "tool", "tool_call_id": "call_valid", "content": "Math is the study of numbers."},
            # Orphaned — no matching assistant.tool_calls
            {"role": "tool", "tool_call_id": "call_ORPHAN_NO_MATCH", "content": "This result has no parent."},
            {"role": "user", "content": "Just answer the math question directly."},
        ]

        resp = _completion(messages, tools=MOCK_TOOLS)
        assert resp.status_code != 400, (
            "Got 400 — LiteLLM hook did NOT clean up the orphaned tool result. "
            "Check _cleanup_orphaned_tool_pairs() in tool-strip-hook-configmap.yaml."
        )
        assert resp.status_code == 200, f"unexpected HTTP {resp.status_code}: {resp.text[:200]}"

    def test_bad_json_cleanup(self):
        """
        tool_call with truncated/invalid JSON arguments (caused by reasoning-budget
        exhaustion mid-generation) must be removed by the hook, not cause a 400.
        """
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user",   "content": "Search for Python history."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id":   "call_truncated",
                    "type": "function",
                    "function": {
                        "name":      "search_web",
                        "arguments": '{"query": "Python programming language hist',  # truncated
                    },
                }],
            },
            {"role": "tool", "tool_call_id": "call_truncated", "content": "Some search result."},
            {"role": "user", "content": "Never mind. What year was Python created?"},
        ]

        resp = _completion(messages, tools=MOCK_TOOLS)
        assert resp.status_code != 400, (
            "Got 400 — hook did NOT strip the truncated-JSON tool_call. "
            "Check _has_invalid_args() in _cleanup_orphaned_tool_pairs()."
        )
        assert resp.status_code == 200, f"unexpected HTTP {resp.status_code}: {resp.text[:200]}"

    def test_mixed_orphan(self):
        """
        Assistant with 2 tool_calls, only 1 result → entire group removed atomically.
        """
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user",   "content": "Search for two topics."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_a", "type": "function",
                     "function": {"name": "search_web", "arguments": '{"query": "topic A"}'}},
                    {"id": "call_b", "type": "function",
                     "function": {"name": "search_web", "arguments": '{"query": "topic B"}'}},
                ],
            },
            # Only call_a has a result; call_b is missing
            {"role": "tool", "tool_call_id": "call_a", "content": "Result for topic A."},
            {"role": "user", "content": "Just summarize what you know."},
        ]

        resp = _completion(messages, tools=MOCK_TOOLS)
        assert resp.status_code != 400, (
            "Got 400 — incomplete tool_call group (2 calls, 1 result) was not removed atomically."
        )
        assert resp.status_code == 200, f"unexpected HTTP {resp.status_code}: {resp.text[:200]}"

    def test_clean_history_passthru(self):
        """Well-formed conversation with complete tool exchange passes through unchanged."""
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")

        call_id = f"call_{uuid.uuid4().hex[:8]}"
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user",   "content": "Search for Python facts."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": "search_web", "arguments": '{"query": "Python facts"}'},
                }],
            },
            {"role": "tool", "tool_call_id": call_id, "content": "Python was created in 1991."},
            {"role": "user", "content": "Great, summarize what you found."},
        ]

        resp = _completion(messages, tools=MOCK_TOOLS)
        assert resp.status_code == 200, f"HTTP {resp.status_code} on clean history: {resp.text[:200]}"
