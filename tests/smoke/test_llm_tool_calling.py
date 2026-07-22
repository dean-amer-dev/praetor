"""
Regression tests for `coder` tool calling via LiteLLM.

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


LITELLM_URL  = os.environ.get("LITELLM_URL", "https://litellm.amer.dev")
# ONE model, ONE runner: murderbot only ever serves sakamakismile/Huihui-Qwen3.6-27B via
# the "coder" LiteLLM alias. MODEL_27B used to point at a separate "qwen36-27b-think" alias;
# that alias never existed after the murderbot-v2 consolidation and every test using it was
# silently failing with HTTP 400 "Invalid model name". Both constants now point at the same
# live alias — there is no second model to distinguish here anymore.
MODEL        = os.environ.get("LLM_MODEL", "coder")
MODEL_27B    = os.environ.get("LLM_MODEL_27B", "coder")
TIMEOUT      = int(os.environ.get("LLM_TIMEOUT", "600"))

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

def _completion(messages, tools=None, max_tokens=2048, model=None, tool_choice=None):
    headers = {"Content-Type": "application/json"}
    if LITELLM_API_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_API_KEY}"
    payload = {"model": model or MODEL, "messages": messages, "max_tokens": max_tokens}
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
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
        force_synthesis = tool_call_count >= _FORCE_SYNTHESIS_AFTER
        active_tools = MOCK_TOOLS
        tc_override = "none" if force_synthesis else None
        resp = _completion(messages, tools=active_tools, tool_choice=tc_override)

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
        Synthesis after 8 tool calls must produce a non-empty response.

        Uses the default MODEL (35B MoE with thinking). The 35B MoE handles
        thinking + synthesis within max_tokens=2048 without exhaustion.
        See test_27b_synthesis_no_thinking_regression for the 27B equivalent.
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

        # Synthesis turn: tools visible but tool_choice="none" (community-standard synthesis signal).
        # Without tool_choice="none", the model sees conversation history with tool calls but no
        # current tools block — it falls back to generating XML <tool_call> syntax instead of prose.
        resp = _completion(messages, tools=MOCK_TOOLS, tool_choice="none", max_tokens=2048)
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

    def test_27b_synthesis_no_thinking_regression(self):
        """
        Regression: qwen36-27b (Qwen3.6-27B, served via the "coder" alias) synthesis
        must return non-empty content.

        Root cause (fixed): vLLM's max_tokens is a total budget shared between thinking
        and output. With thinking enabled and max_tokens=1000, the model exhausted the
        budget thinking and produced empty content. Fix: vllm-server now sets
        --default-chat-template-kwargs '{"enable_thinking": false}' so thinking is off
        by default for every alias, including "coder".

        This test re-runs the exact failure scenario (8 tool turns + synthesis at
        max_tokens=1000) to catch regressions if thinking is re-enabled on this model.
        """
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")

        big_result = (
            "Laptops for elderly parents: Acer Swift Go 14 ($650) lightweight, "
            "HP Pavilion ($549) large screen, Dell Inspiron 15 ($499) reliable. "
            "Key features: large display, backlit keyboard, long battery life."
        ) * 8  # ~1400 chars, realistic tool result size

        messages = [
            {"role": "system", "content": "Be helpful. After gathering info, write your answer."},
            {"role": "user", "content": "What are the best laptops for elderly parents?"},
        ]
        for i in range(8):
            cid = f"call_{i:04d}"
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{"type": "function", "id": cid,
                                "function": {"name": "search_web", "arguments": '{"query":"test"}'}}],
            })
            messages.append({"role": "tool", "tool_call_id": cid, "content": big_result})

        # Synthesis turn: no tools, small max_tokens — the exact failure scenario.
        resp = _completion(messages, tools=None, max_tokens=1000, model=MODEL_27B)
        assert resp.status_code == 200, f"synthesis HTTP {resp.status_code}: {resp.text[:300]}"

        choice  = resp.json()["choices"][0]
        content = choice["message"].get("content") or ""
        reasoning = choice["message"].get("reasoning_content") or ""
        finish  = choice["finish_reason"]

        assert len(content) >= 100, (
            f"27B synthesis empty or too short ({len(content)} chars, finish={finish}, "
            f"reasoning_len={len(reasoning)}). "
            "Re-check vllm-server's --default-chat-template-kwargs: enable_thinking must be false."
        )

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

    def test_synthesis_xml_leakage_rate(self):
        """
        Repeated-trial characterization of XML leakage on the synthesis turn.

        Even with tool_choice="none" (the documented synthesis signal), the model
        occasionally emits a raw "<tool_call>...</tool_call>"-looking string as content
        instead of prose — observed live during a stress audit (2 of 12 individual turns)
        and reproduced by test_complex_synthesis_non_empty in this file. This isn't a
        deterministic pass/fail regression, it's model-sampling nondeterminism — so instead
        of asserting zero leakage (flaky) or ignoring it (silently getting worse over time),
        this runs N independent synthesis trials and fails only if the leak rate crosses a
        threshold, giving a concrete, trackable signal instead of an open question.
        """
        if not LITELLM_API_KEY:
            pytest.skip("LITELLM_API_KEY not available")

        N = 10
        MAX_LEAK_RATE = 0.3  # fail if more than 30% of trials leak — well above the ~17% observed live

        leaks = 0
        for i in range(N):
            messages = [
                {"role": "system", "content": "Be helpful. After gathering info, write your answer."},
                {"role": "user", "content": f"What are the best laptops for elderly parents? (trial {i})"},
            ]
            cid = f"call_{i:04d}"
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{"type": "function", "id": cid,
                                "function": {"name": "search_web", "arguments": '{"query":"laptops elderly"}'}}],
            })
            messages.append({
                "role": "tool", "tool_call_id": cid,
                "content": MOCK_TOOL_RESULTS["search_web"],
            })

            resp = _completion(messages, tools=MOCK_TOOLS, tool_choice="none", max_tokens=512)
            if resp.status_code != 200:
                continue  # transport-level failures are covered by other tests, not this one
            content = resp.json()["choices"][0]["message"].get("content") or ""
            if "<tool_call>" in content or "<function=" in content:
                leaks += 1

        leak_rate = leaks / N
        assert leak_rate <= MAX_LEAK_RATE, (
            f"XML leakage rate {leak_rate:.0%} ({leaks}/{N} trials) exceeds the {MAX_LEAK_RATE:.0%} "
            "threshold. tool_choice='none' is meant to force prose synthesis — if leakage is "
            "climbing, something regressed in tool-call formatting or the synthesis prompt."
        )
