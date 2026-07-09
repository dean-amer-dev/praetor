"""
OWU tool pipeline smoke tests.

Run: SMOKE_TESTS=1 pytest tests/smoke/test_owui_tool_pipeline.py -v

## Architecture

The OWU /api/v1/chat/completions endpoint does not auto-inject toolIds from the
model config — that only happens via the browser UI pipeline. So these tests pass
tool definitions explicitly, then execute the multi-turn loop themselves (call the
tool, feed results back). This tests the same behavior the UI exercises.

## Test classes

TestModelConfig   — fast config checks (no LLM calls). Catch restart-wipe regressions.
TestEndToEnd      — full tool execution loop. The tests that actually matter.
TestLiteLLMMCP    — LiteLLM MCP gateway: reachable, tools present, date injection.
TestWebSearch     — SearXNG reachable and configured.
TestPraetorAPI    — Praetor API accepts key and returns task IDs.
"""
import json
import os
import time
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.smoke

if not os.environ.get("SMOKE_TESTS"):
    pytest.skip("Set SMOKE_TESTS=1 to run OWU pipeline tests", allow_module_level=True)


# ── Config ────────────────────────────────────────────────────────────────────

OWUI_URL      = os.environ.get("OWUI_URL", "https://bot.amer.dev")
OWUI_EMAIL    = os.environ.get("OWUI_ADMIN_EMAIL", "alex@amer.dev")
OWUI_PASSWORD = os.environ.get("OWUI_ADMIN_PASSWORD", "foRdJI1ZsEESVrQl5R1l")

LITELLM_URL   = os.environ.get("LITELLM_URL", "https://litellm.amer.dev")
LITELLM_KEY   = os.environ.get("LITELLM_API_KEY", "fmxVy6bPQTClCDy9QOsjBMN3sfScX38JpjlyUv9Q")

PRAETOR_URL   = os.environ.get("PRAETOR_URL", "https://praetor.amer.dev")
PRAETOR_KEY   = os.environ.get("PRAETOR_API_KEY", "dRykVJyZp79Ute6JRKlZAgTuMs2jMXodKpszRyj-8aY")

SEARXNG_URL   = os.environ.get("SEARXNG_URL", "https://searxng.amer.dev")

CUSTOM_MODEL  = "qwen3-35b-think-custom"
BASE_MODEL    = "qwen3-35b-think"

V1_CUSTOM_MODEL = "qwen3-27b-think-custom"
V1_BASE_MODEL   = "qwen36-27b-think"

TIMEOUT       = int(os.environ.get("LLM_TIMEOUT", "120"))


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def owui_token():
    resp = httpx.post(
        f"{OWUI_URL}/api/v1/auths/signin",
        json={"email": OWUI_EMAIL, "password": OWUI_PASSWORD},
        timeout=10,
    )
    assert resp.status_code == 200, f"OWU login failed: {resp.status_code}"
    return resp.json()["token"]


@pytest.fixture(scope="module")
def owui(owui_token):
    return httpx.Client(
        base_url=OWUI_URL,
        headers={"Authorization": f"Bearer {owui_token}"},
        timeout=TIMEOUT,
    )


@pytest.fixture(scope="module")
def litellm():
    return httpx.Client(
        base_url=LITELLM_URL,
        headers={
            "Authorization": f"Bearer {LITELLM_KEY}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        timeout=TIMEOUT,
    )


@pytest.fixture(scope="module")
def mcp_tool_defs(litellm) -> list[dict]:
    """Fetch live tool definitions from LiteLLM MCP. Fails if MCP is unreachable."""
    resp = litellm.post(
        "/mcp/",
        content=b'{"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}',
    )
    assert resp.status_code == 200, f"LiteLLM MCP tools/list failed: {resp.status_code}: {resp.text[:300]}"
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])["result"]["tools"]
    pytest.fail("Could not parse MCP tools/list SSE response")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_openai(mcp_tool: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": mcp_tool["name"],
            "description": mcp_tool["description"],
            "parameters": mcp_tool["inputSchema"],
        },
    }


def _select_tools(mcp_tool_defs: list[dict], names: set[str]) -> list[dict]:
    return [_to_openai(t) for t in mcp_tool_defs if t["name"] in names]


def _dispatch_tool_def() -> dict:
    """OpenAI-format dispatch_task definition for inclusion in tool lists."""
    return {
        "type": "function",
        "function": {
            "name": "dispatch_task",
            "description": (
                "Dispatch a background agent task. "
                "Use ONLY for coding tasks — implement, fix bugs, modify files, open PRs. "
                "NEVER use for research or questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "task_type": {"type": "string", "enum": ["openhands", "code", "pipeline"]},
                },
                "required": ["title", "description", "task_type"],
            },
        },
    }


def _execute_mcp_tool(litellm: httpx.Client, name: str, args: dict) -> str:
    """Execute a tool via LiteLLM MCP and return the text result."""
    resp = litellm.post(
        "/mcp/",
        content=json.dumps({
            "jsonrpc": "2.0", "id": "exec",
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }).encode(),
        timeout=30,
    )
    assert resp.status_code == 200, (
        f"MCP tools/call failed for {name!r}: HTTP {resp.status_code}: {resp.text[:300]}"
    )
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            content_list = json.loads(line[6:]).get("result", {}).get("content", [])
            return " ".join(c.get("text", "") for c in content_list if c.get("type") == "text")
    return ""


def _run_tool_loop(
    owui: httpx.Client,
    litellm: httpx.Client,
    messages: list[dict],
    tools: list[dict],
    max_tool_turns: int = 5,
    model: str = CUSTOM_MODEL,
) -> tuple[str, list[str], bool]:
    """
    Run a multi-turn tool conversation.

    Executes tool calls via LiteLLM MCP and feeds results back until the model
    produces a final text answer.

    First turn uses tool_choice='required' to force JSON tool_calls (with 'auto',
    this model defaults to text-injection XML). After max_tool_turns, sends a
    final synthesis turn with no tools so the model must write its answer.

    Returns: (final_content, tool_names_called, had_xml)
    """
    tool_names_called: list[str] = []
    had_xml = False

    for turn in range(max_tool_turns):
        tool_choice = "required" if turn == 0 else "auto"
        resp = owui.post(
            "/api/v1/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "stream": False,
                "max_tokens": 800,
            },
        )
        assert resp.status_code == 200, f"OWU HTTP {resp.status_code}: {resp.text[:300]}"

        choice = resp.json()["choices"][0]
        msg = choice["message"]
        content = msg.get("content") or ""
        tc = msg.get("tool_calls") or []

        if "<function=" in content:
            had_xml = True

        if not tc:
            # Model chose to answer — return it
            return content, tool_names_called, had_xml

        messages.append(msg)
        for call in tc:
            name = call["function"]["name"]
            args = json.loads(call["function"]["arguments"])
            tool_names_called.append(name)
            result = _execute_mcp_tool(litellm, name, args)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

    # Model used all tool turns — force it to synthesize now.
    # max_tokens=1024: qwen36-27b-think has a 16K context limit. After 5+ real
    # tool calls, the conversation can grow to ~14K input tokens. Using 2048 here
    # would push the total to 16385 → 400 ContextWindowExceededError. 1024 keeps
    # us within budget (14337 + 1024 = 15361 < 16384) while still producing a
    # substantive answer (the >100 char assertion is easily satisfied at 1024 tokens).
    resp = owui.post(
        "/api/v1/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "stream": False,
            "max_tokens": 1024,
        },
    )
    assert resp.status_code == 200, f"OWU synthesis turn HTTP {resp.status_code}: {resp.text[:300]}"
    final_content = resp.json()["choices"][0]["message"].get("content") or ""
    return final_content, tool_names_called, had_xml


# ── Config correctness (fast, no LLM calls) ───────────────────────────────────

class TestModelConfig:
    """Verify OWU model config survives restarts. Fast — no LLM calls."""

    def test_custom_model_exists(self, owui):
        resp = owui.get(f"/api/v1/models/model?id={CUSTOM_MODEL}")
        assert resp.status_code == 200, f"Custom model not found: HTTP {resp.status_code}"
        assert resp.json().get("name") == "murderbot-v0"

    def test_function_calling_native(self, owui):
        """function_calling must be 'native' — otherwise model outputs XML that OWU cannot execute."""
        resp = owui.get(f"/api/v1/models/model?id={CUSTOM_MODEL}")
        assert resp.status_code == 200
        fc = resp.json().get("params", {}).get("function_calling")
        assert fc == "native", (
            f"function_calling={fc!r}. Must be 'native' — text injection produces "
            "<function=...> XML that OWU's parser ignores, silently breaking all tool calls."
        )

    def test_praetor_dispatch_in_tool_ids(self, owui):
        resp = owui.get(f"/api/v1/models/model?id={CUSTOM_MODEL}")
        assert resp.status_code == 200
        tool_ids = resp.json().get("meta", {}).get("toolIds", [])
        assert "praetor_dispatch" in tool_ids, f"praetor_dispatch missing from toolIds: {tool_ids}"

    def test_server_mcp_lm_in_tool_ids(self, owui):
        """server:mcp:lm must be in toolIds — this is what provides lm_searxng_search etc."""
        resp = owui.get(f"/api/v1/models/model?id={CUSTOM_MODEL}")
        assert resp.status_code == 200
        tool_ids = resp.json().get("meta", {}).get("toolIds", [])
        assert "server:mcp:lm" in tool_ids, (
            f"server:mcp:lm missing from toolIds: {tool_ids}. Run scripts/register_owui_tool.py."
        )

    def test_builtin_web_search_disabled(self, owui):
        """builtinTools.web_search must be False — web search comes from server:mcp:lm, not OWU builtins."""
        resp = owui.get(f"/api/v1/models/model?id={CUSTOM_MODEL}")
        assert resp.status_code == 200
        ws = resp.json().get("meta", {}).get("builtinTools", {}).get("web_search")
        assert ws is False, (
            f"builtinTools.web_search={ws!r}. Should be False — use server:mcp:lm instead."
        )

    def test_system_prompt_present(self, owui):
        resp = owui.get(f"/api/v1/models/model?id={CUSTOM_MODEL}")
        assert resp.status_code == 200
        system = resp.json().get("meta", {}).get("system", "")
        assert len(system) > 50, f"system prompt missing or too short ({len(system)} chars)"

    def test_date_injector_filter_active_and_global(self, owui):
        """date_injector filter must be active and global — prepends today's date to every system prompt."""
        resp = owui.get("/api/v1/functions/")
        assert resp.status_code == 200
        f = next((x for x in resp.json() if x.get("id") == "date_injector"), None)
        assert f is not None, "date_injector filter not found. Run scripts/register_owui_tool.py."
        assert f.get("is_active") is True, f"date_injector is disabled (is_active={f.get('is_active')})"
        assert f.get("is_global") is True, f"date_injector is not global (is_global={f.get('is_global')})"

    def test_base_model_active(self, owui):
        """qwen3-35b-think must be active — OWU 0.9.6 requires base model active to route custom model completions."""
        resp = owui.get(f"/api/v1/models/model?id={BASE_MODEL}")
        assert resp.status_code == 200, f"Base model not found: HTTP {resp.status_code}"
        assert resp.json().get("is_active") is True, f"Base model {BASE_MODEL} is inactive"

    def test_praetor_dispatch_tool_registered(self, owui):
        """praetor_dispatch Python tool must exist with a non-empty API key."""
        resp = owui.get("/api/v1/tools/")
        assert resp.status_code == 200
        t = next((x for x in resp.json() if x.get("id") == "praetor_dispatch"), None)
        assert t is not None, "praetor_dispatch tool not found in OWU"
        content = t.get("content", "")
        assert "dispatch_task" in content, "dispatch_task method missing from praetor_dispatch"
        assert "dRyk" in content or "PRAETOR_API_KEY" in content, "praetor_dispatch has no API key"


# ── End-to-end tool execution ─────────────────────────────────────────────────

class TestEndToEnd:
    """
    Full multi-turn tool execution tests. These are the tests that matter.

    Each test:
    1. Fetches live tool definitions from LiteLLM MCP (fails if MCP is down)
    2. Sends a message to the model with those tools
    3. Executes tool calls via LiteLLM MCP, feeds results back
    4. Verifies the final answer is substantive and came from the right path

    Catches: XML output, wrong tool called, MCP not executing, empty answers.
    """

    def test_tool_calls_are_json_not_xml(self, owui, mcp_tool_defs):
        """
        Model must return tool_calls JSON, never <function=...> XML.

        XML means OWU is in text injection mode. OWU cannot parse or execute XML
        tool calls — they appear as raw text in the chat, silently breaking tool use.
        """
        tools = _select_tools(mcp_tool_defs, {"searxng_search", "searxng_read_url"})
        resp = owui.post(
            "/api/v1/chat/completions",
            json={
                "model": CUSTOM_MODEL,
                "messages": [{"role": "user", "content": "Search for recent Python 3.13 release notes."}],
                "tools": tools,
                "tool_choice": "required",
                "stream": False,
                "max_tokens": 200,
            },
        )
        assert resp.status_code == 200
        choice = resp.json()["choices"][0]
        msg = choice["message"]
        content = msg.get("content") or ""

        assert "<function=" not in content, (
            f"Model produced XML tool call — not using native JSON.\n"
            f"content: {content[:400]}\n"
            "Check: function_calling='native' on the model config."
        )
        assert choice["finish_reason"] == "tool_calls", (
            f"finish_reason={choice['finish_reason']!r}, expected 'tool_calls'. "
            f"content: {content[:200]!r}"
        )
        assert msg.get("tool_calls"), "tool_calls field is empty despite finish_reason='tool_calls'"

    def test_research_uses_searxng_search_not_dispatch(self, owui, litellm, mcp_tool_defs):
        """
        Research question must use searxng_search and return a real answer, never dispatch.

        Verifies the full pipeline: model calls searxng_search → MCP executes → results
        fed back → model writes a substantive answer with actual content.

        Uses tool_choice='required' on the first turn because with 'auto' this model
        defaults to text-injection XML format which the API cannot execute. 'required'
        forces the model into native JSON tool_calls. Subsequent turns use 'auto'.

        Fails if: model dispatches instead of searching, XML output, MCP tool broken,
        or answer is empty/placeholder.
        """
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        tools = _select_tools(mcp_tool_defs, {"searxng_search", "searxng_read_url"})
        tools.append(_dispatch_tool_def())

        # Pass system prompt explicitly — OWU API doesn't guarantee model config
        # system prompt is applied, and we need the behavioral guidance.
        messages = [
            {
                "role": "system",
                "content": (
                    f"Today's date is {today} (UTC).\n"
                    "You are a helpful assistant. For research questions, call searxng_search immediately. "
                    "For coding tasks, call dispatch_task. Never dispatch for research."
                ),
            },
            {"role": "user", "content": "What is the latest stable release of llama.cpp? Search for it."},
        ]
        final, called, had_xml = _run_tool_loop(owui, litellm, messages, tools)

        assert not had_xml, (
            "Model produced <function=...> XML in response content — native tool calling is broken."
        )
        assert "dispatch_task" not in called, (
            f"Model called dispatch_task for a research question. Called: {called}. "
            "System prompt says dispatch is only for coding tasks."
        )
        assert any(n in ("searxng_search", "searxng_read_url") for n in called), (
            f"Model did not call any web search tool. Called: {called}. "
            "Check server:mcp:lm is in toolIds and LiteLLM MCP is reachable."
        )
        assert len(final) > 80, (
            f"Final answer too short ({len(final)} chars) — model may not have used search results.\n"
            f"Answer: {final!r}\nTools called: {called}"
        )

    def test_mcp_searxng_search_executes_and_returns_results(self, litellm, mcp_tool_defs):
        """
        LiteLLM MCP must actually execute searxng_search and return non-empty results.

        Tests the execution path independently from the model. If this fails, MCP
        tool execution is broken regardless of what the model does.

        Uses a generic query ("Python programming language") that reliably returns
        results from any major search engine — avoids flaky failures on niche queries
        when SearXNG engines are rate-limited or temporarily down.
        """
        result = _execute_mcp_tool(litellm, "searxng_search", {"query": "Python programming language", "max_results": 3})
        assert result and len(result) > 50, (
            f"searxng_search returned no content ({len(result)} chars): {result!r}. "
            "SearXNG or LiteLLM MCP execution may be broken."
        )
        assert "python" in result.lower(), (
            f"searxng_search result doesn't mention 'python' for a python query: {result[:300]!r}"
        )

    def test_date_injected_into_system_prompt(self, owui):
        """
        date_injector filter must fire — model must know today's date without searching for it.

        Uses max_tokens=800 because reasoning mode consumes ~300-500 tokens before
        producing any content. Lower values result in finish_reason='length' with
        empty content even for trivial questions.
        """
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        resp = owui.post(
            "/api/v1/chat/completions",
            json={
                "model": CUSTOM_MODEL,
                # No "tools" key — vLLM rejects empty arrays ("tools must not be an
                # empty array; either provide at least one tool or omit the field").
                "messages": [{"role": "user", "content": "What ISO date does your system context say it is? Reply with just the date, nothing else."}],
                "stream": False,
                "max_tokens": 800,
            },
        )
        assert resp.status_code == 200
        choice = resp.json()["choices"][0]
        assert choice["finish_reason"] != "length", (
            "Model hit token limit before producing content — increase max_tokens in this test"
        )
        content = choice["message"].get("content", "") or ""
        assert today in content, (
            f"Model did not report today's date {today!r}. Got: {content!r}. "
            "date_injector filter may be inactive or not global."
        )

    def test_research_produces_synthesis(self, owui, litellm, mcp_tool_defs):
        """
        Research question must produce a real text answer after tool calls.

        Root issue: user asked about Jellyfin + Google displays. Model called 18+ tools,
        returned blank. Originally fixed by a bespoke tool-strip-hook (now removed).

        Correct fix: forced synthesis (tools=None after N tool turns) — the community-standard
        approach per OpenAI function-calling docs. Without thinking enabled, a 27B dense model
        won't reliably count tool calls and self-terminate; the client enforces the budget.
        _run_tool_loop implements this: up to max_tool_turns with tools, then one final
        synthesis call without tools.

        What this test validates: the full research pipeline works end-to-end — model gathers
        information via real MCP tools and produces a substantive text answer.
        """
        RESEARCH_TOOLS = {"searxng_search", "searxng_read_url", "praetor_memory_search"}
        tools = _select_tools(mcp_tool_defs, RESEARCH_TOOLS)
        tools.append(_dispatch_tool_def())

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        model_resp = owui.get(f"/api/v1/models/model?id={CUSTOM_MODEL}")
        assert model_resp.status_code == 200, "Could not fetch custom model config"
        system_prompt = model_resp.json().get("meta", {}).get("system", "")
        assert system_prompt, "Custom model has no system prompt — run scripts/register_owui_tool.py"

        messages = [
            {"role": "system", "content": f"Today's date is {today} (UTC).\n{system_prompt}"},
            {"role": "user", "content": (
                "please do some research on how I can connect jellyfin to Google displays "
                "so I can use my wake word and have it play an episode or movie from jellyfin"
            )},
        ]

        final_content, tool_calls_made, had_xml = _run_tool_loop(
            owui, litellm, messages, tools, max_tool_turns=6,
        )

        assert not had_xml, (
            f"Model used XML tool syntax — function_calling must be 'native' on {CUSTOM_MODEL}"
        )
        assert len(tool_calls_made) >= 1, (
            "Model never called any tools for a research question — check toolIds config."
        )
        assert len(final_content) > 100, (
            f"Research synthesis too short ({len(final_content)} chars).\n"
            f"Tools called: {tool_calls_made}\nContent: {final_content!r}"
        )

    def test_coding_task_dispatches_not_searches(self, owui, litellm, mcp_tool_defs):
        """
        Coding task must call dispatch_task, not web_search.

        This is the complement of test_research_uses_web_search_not_dispatch.
        Verifies the model routes correctly in both directions.

        System prompt is passed explicitly — see test_research_uses_web_search_not_dispatch
        for why this is required rather than relying on the OWU model config being applied.
        """
        tools = _select_tools(mcp_tool_defs, {"searxng_search", "searxng_read_url"})
        tools.append(_dispatch_tool_def())

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant. "
                    "For coding tasks (implement features, fix bugs, modify files, open PRs): "
                    "call dispatch_task with task_type='openhands' and include 'repo: owner/name'. "
                    "Do NOT write code yourself. Do NOT search the web for coding tasks. "
                    "For research questions: call searxng_search."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Add a /healthz endpoint to amerenda/praetor that returns {\"ok\": true}. "
                    "repo: amerenda/praetor"
                ),
            },
        ]

        resp = owui.post(
            "/api/v1/chat/completions",
            json={
                "model": CUSTOM_MODEL,
                "messages": messages,
                "tools": tools,
                "tool_choice": "required",
                "stream": False,
                "max_tokens": 400,
                "temperature": 0,
            },
        )
        assert resp.status_code == 200
        choice = resp.json()["choices"][0]
        msg = choice["message"]
        content = msg.get("content") or ""
        tc = msg.get("tool_calls") or []

        assert "<function=" not in content, "Model produced XML output — not using native tool_calls"
        assert choice["finish_reason"] == "tool_calls", (
            f"finish_reason={choice['finish_reason']!r}. content: {content[:200]!r}"
        )

        called_names = [c["function"]["name"] for c in tc]
        assert "dispatch_task" in called_names, (
            f"Model called wrong tool(s) for coding task: {called_names}. Expected dispatch_task. "
            "Check the system prompt instructs dispatch for coding tasks."
        )
        assert "searxng_search" not in called_names, (
            f"Model searched instead of dispatching for a coding task: {called_names}"
        )


# ── LiteLLM MCP gateway ───────────────────────────────────────────────────────

class TestLiteLLMMCP:

    def test_litellm_mcp_reachable(self, litellm):
        resp = litellm.post("/mcp/", content=b'{"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}')
        assert resp.status_code == 200, f"LiteLLM MCP HTTP {resp.status_code}: {resp.text[:300]}"

    def test_litellm_mcp_exposes_searxng_search(self, mcp_tool_defs):
        names = {t["name"] for t in mcp_tool_defs}
        assert "searxng_search" in names, f"searxng_search missing from LiteLLM MCP tools: {names}"
        assert "searxng_read_url" in names, f"searxng_read_url missing from LiteLLM MCP tools: {names}"

    def test_litellm_serves_base_model(self, litellm):
        resp = litellm.get("/v1/models", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["data"]]
        assert BASE_MODEL in ids, f"{BASE_MODEL!r} not in LiteLLM models: {ids}"

    def test_litellm_native_tool_call_baseline(self, litellm, mcp_tool_defs):
        """Direct LiteLLM call must return tool_calls JSON. If this fails, llama.cpp tool calling is broken."""
        tools = _select_tools(mcp_tool_defs, {"searxng_search"})
        resp = litellm.post(
            "/v1/chat/completions",
            json={
                "model": BASE_MODEL,
                "messages": [{"role": "user", "content": "Search for recent llama.cpp releases"}],
                "tools": tools,
                "tool_choice": "auto",
                "max_tokens": 256,
            },
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200, f"LiteLLM HTTP {resp.status_code}: {resp.text[:300]}"
        choice = resp.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls", (
            f"finish_reason={choice['finish_reason']!r}. content: {choice['message'].get('content','')[:200]!r}"
        )
        assert choice["message"].get("tool_calls"), "tool_calls field missing"


# ── SearXNG ───────────────────────────────────────────────────────────────────

class TestWebSearch:

    def test_searxng_reachable_and_returns_results(self):
        resp = httpx.get(f"{SEARXNG_URL}/search", params={"q": "llama.cpp", "format": "json"}, timeout=10)
        assert resp.status_code == 200, f"SearXNG HTTP {resp.status_code}"
        assert len(resp.json().get("results", [])) > 0, "SearXNG returned 0 results"

    def test_owui_searxng_config_reachable(self, owui):
        resp = owui.get("/api/v1/retrieval/config")
        assert resp.status_code == 200
        url = resp.json().get("web", {}).get("SEARXNG_QUERY_URL", "")
        assert url, "SEARXNG_QUERY_URL is empty in OWU config"
        base = url.split("?")[0].rsplit("/search", 1)[0]
        health = httpx.get(f"{base}/search", params={"q": "test", "format": "json"}, timeout=10)
        assert health.status_code == 200, f"SearXNG at {base!r} returned HTTP {health.status_code}"


# ── Praetor API ───────────────────────────────────────────────────────────────

class TestPraetorAPI:

    def test_praetor_dispatch_accepts_key(self):
        resp = httpx.post(
            f"{PRAETOR_URL}/api/v1/dispatch",
            json={"title": "smoke-test ping", "description": "Automated smoke test — safe to ignore", "type": "research"},
            headers={"Authorization": f"Bearer {PRAETOR_KEY}"},
            timeout=15,
        )
        assert resp.status_code == 200, f"Praetor dispatch HTTP {resp.status_code}: {resp.text[:200]}"
        assert "task_id" in resp.json(), f"No task_id in response: {resp.json()}"

    def test_praetor_status_endpoint_works(self):
        create = httpx.post(
            f"{PRAETOR_URL}/api/v1/dispatch",
            json={"title": "status-check", "description": "smoke test", "type": "research"},
            headers={"Authorization": f"Bearer {PRAETOR_KEY}"},
            timeout=15,
        )
        assert create.status_code == 200
        task_id = create.json()["task_id"]
        status = httpx.get(
            f"{PRAETOR_URL}/api/v1/status/{task_id}",
            headers={"Authorization": f"Bearer {PRAETOR_KEY}"},
            timeout=10,
        )
        assert status.status_code == 200
        assert status.json().get("task_id") == task_id


# ── murderbot-v1 (Qwen3.6-27B) ───────────────────────────────────────────────

class TestMurderbotV1:
    """
    Smoke tests for murderbot-v1 (qwen3-27b-think-custom / qwen36-27b-think).

    Covers tool calling, no XML leakage, and full research loop with real results.
    """

    def test_v1_model_exists_in_owu(self, owui):
        resp = owui.get(f"/api/v1/models/model?id={V1_CUSTOM_MODEL}")
        assert resp.status_code == 200, (
            f"murderbot-v1 ({V1_CUSTOM_MODEL}) not found: HTTP {resp.status_code}"
        )
        assert resp.json().get("name") == "murderbot-v1"

    def test_v1_function_calling_native(self, owui):
        """function_calling must be 'native' — text injection produces XML blobs in content."""
        resp = owui.get(f"/api/v1/models/model?id={V1_CUSTOM_MODEL}")
        assert resp.status_code == 200
        fc = resp.json().get("params", {}).get("function_calling")
        assert fc == "native", (
            f"murderbot-v1 function_calling={fc!r} — must be 'native'. "
            "XML tool call blobs will leak into content otherwise."
        )

    def test_v1_base_model_in_litellm(self, litellm):
        """qwen36-27b-think must be registered in LiteLLM."""
        resp = litellm.get("/v1/models", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["data"]]
        assert V1_BASE_MODEL in ids, (
            f"{V1_BASE_MODEL!r} not in LiteLLM: {ids}"
        )

    def test_v1_no_xml_tool_calls(self, owui, mcp_tool_defs):
        """
        Model must return JSON tool_calls, never <function=...> XML in content.

        Uses tool_choice='required' to force a tool call response.
        """
        tools = _select_tools(mcp_tool_defs, {"searxng_search", "searxng_read_url"})
        resp = owui.post(
            "/api/v1/chat/completions",
            json={
                "model": V1_CUSTOM_MODEL,
                "messages": [{"role": "user", "content": "Search for best laptops for elderly people."}],
                "tools": tools,
                "tool_choice": "required",
                "stream": False,
                "max_tokens": 400,
            },
        )
        assert resp.status_code == 200, f"OWU HTTP {resp.status_code}: {resp.text[:300]}"
        choice = resp.json()["choices"][0]
        msg    = choice["message"]
        content = msg.get("content") or ""

        assert "<function=" not in content, (
            f"murderbot-v1 produced XML in content:\n{content[:500]}\n"
            "Set function_calling='native' on the OWU preset."
        )
        assert choice["finish_reason"] == "tool_calls", (
            f"finish_reason={choice['finish_reason']!r}, content={content[:200]!r}"
        )
        assert msg.get("tool_calls"), "tool_calls field is empty"

    def test_v1_model_config(self, owui):
        """
        Composite config check for murderbot-v1 — parallels TestModelConfig for v0.

        Catches restart-wipe regressions where OWU loses custom model settings.
        Checks: model exists, function_calling=native, toolIds have required entries,
        system prompt present.
        """
        resp = owui.get(f"/api/v1/models/model?id={V1_CUSTOM_MODEL}")
        assert resp.status_code == 200, (
            f"murderbot-v1 ({V1_CUSTOM_MODEL}) not found: HTTP {resp.status_code}. "
            "Run scripts/register_owui_tool.py to restore it."
        )
        data = resp.json()

        assert data.get("name") == "murderbot-v1", (
            f"Expected name='murderbot-v1', got {data.get('name')!r}"
        )

        fc = data.get("params", {}).get("function_calling")
        assert fc == "native", (
            f"function_calling={fc!r} on murderbot-v1 — must be 'native'. "
            "Text injection produces <function=...> XML that OWU cannot execute."
        )

        tool_ids = data.get("meta", {}).get("toolIds", [])
        assert "praetor_dispatch" in tool_ids, (
            f"praetor_dispatch missing from murderbot-v1 toolIds: {tool_ids}"
        )
        assert "server:mcp:lm" in tool_ids, (
            f"server:mcp:lm missing from murderbot-v1 toolIds: {tool_ids}. "
            "This provides lm_searxng_search and other research tools."
        )

        system = data.get("meta", {}).get("system", "")
        assert len(system) > 50, (
            f"murderbot-v1 system prompt missing or too short ({len(system)} chars)"
        )

    def test_v1_research_produces_synthesis(self, owui, litellm, mcp_tool_defs):
        """
        Research question via murderbot-v1 must produce a substantive text answer.

        This is the test that would have caught the live regression: user sent a
        research question, model called tools, response returned empty content
        (finish_reason=stop but no text).

        Root cause hypothesis: qwen36-27b-think exhausts max_tokens budget on thinking
        during synthesis turn. Fix: enable_thinking=false in LiteLLM model config.

        What this validates end-to-end:
        - OWU routes qwen3-27b-think-custom → LiteLLM → llama.cpp
        - Model calls real MCP tools (not mocks)
        - Synthesis turn returns >100 chars of actual content
        """
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        RESEARCH_TOOLS = {"searxng_search", "searxng_read_url", "praetor_memory_search"}
        tools = _select_tools(mcp_tool_defs, RESEARCH_TOOLS)
        tools.append(_dispatch_tool_def())

        model_resp = owui.get(f"/api/v1/models/model?id={V1_CUSTOM_MODEL}")
        assert model_resp.status_code == 200, "Could not fetch murderbot-v1 config"
        system_prompt = model_resp.json().get("meta", {}).get("system", "")
        assert system_prompt, "murderbot-v1 has no system prompt"

        messages = [
            {"role": "system", "content": f"Today's date is {today} (UTC).\n{system_prompt}"},
            {"role": "user", "content": (
                "please do some research on how I can connect jellyfin to Google displays "
                "so I can use my wake word and have it play an episode or movie from jellyfin"
            )},
        ]

        final_content, tool_calls_made, had_xml = _run_tool_loop(
            owui, litellm, messages, tools, max_tool_turns=5, model=V1_CUSTOM_MODEL,
        )

        assert not had_xml, (
            f"murderbot-v1 used XML tool syntax — function_calling must be 'native' on {V1_CUSTOM_MODEL}"
        )
        assert len(tool_calls_made) >= 1, (
            "murderbot-v1 never called any tools for a research question — check toolIds config."
        )
        assert len(final_content) > 100, (
            f"murderbot-v1 research synthesis too short ({len(final_content)} chars).\n"
            f"Tools called: {tool_calls_made}\nContent: {final_content!r}\n"
            "If empty: check qwen36-27b-think has chat_template_kwargs.enable_thinking=false "
            "in LiteLLM config (apps/litellm/server/configmap.yaml). Thinking exhausts "
            "max_tokens budget and produces empty content."
        )

    def test_v1_synthesis_token_budget(self, owui, litellm, mcp_tool_defs):
        """
        Diagnose thinking-budget exhaustion: 5 pre-baked tool turns + synthesis at max_tokens=1500.

        Reproduces the exact failure mode: if qwen36-27b-think has thinking enabled,
        the model spends its entire token budget on <think> blocks and returns empty content.
        Fix: chat_template_kwargs.enable_thinking=false in LiteLLM config.

        Uses pre-baked tool turns (no live MCP calls) so this test runs fast and
        isolates the synthesis-budget issue from network/MCP availability.
        """
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        big_result = (
            "Laptops for elderly parents: Acer Swift Go 14 ($650) lightweight, "
            "HP Pavilion ($549) large screen, Dell Inspiron 15 ($499) reliable. "
            "Key features: large display, backlit keyboard, long battery life, "
            "simple OS (ChromeOS or Windows), voice assistant support."
        ) * 5  # ~900 chars, realistic tool result size

        messages = [
            {
                "role": "system",
                "content": (
                    f"Today's date is {today} (UTC).\n"
                    "You are a helpful research assistant. After gathering information, "
                    "write a comprehensive, well-structured answer for the user."
                ),
            },
            {"role": "user", "content": "What are the best laptops for elderly parents?"},
        ]

        # Inject 5 pre-baked tool turns — same pattern as test_27b_synthesis_no_thinking_regression
        for i in range(5):
            cid = f"call_{i:04d}"
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "type": "function",
                    "id": cid,
                    "function": {"name": "searxng_search", "arguments": '{"query":"laptops elderly parents"}'},
                }],
            })
            messages.append({"role": "tool", "tool_call_id": cid, "content": big_result})

        # Synthesis call via OWU: no tools, max_tokens=1500 — tight enough to expose thinking budget exhaustion
        resp = owui.post(
            "/api/v1/chat/completions",
            json={
                "model": V1_CUSTOM_MODEL,
                "messages": messages,
                "stream": False,
                "max_tokens": 1500,
            },
        )
        assert resp.status_code == 200, f"OWU synthesis HTTP {resp.status_code}: {resp.text[:300]}"

        choice = resp.json()["choices"][0]
        content = choice["message"].get("content") or ""
        finish = choice["finish_reason"]

        assert len(content) >= 100, (
            f"27B synthesis empty — check qwen36-27b-think has enable_thinking=false in LiteLLM config.\n"
            f"Got {len(content)} chars (finish_reason={finish!r}).\n"
            f"Content: {content[:200]!r}\n"
            "Fix: add chat_template_kwargs: {{enable_thinking: false}} to the qwen36-27b-think "
            "entry in apps/litellm/server/configmap.yaml in k3s-dean-gitops."
        )

    def test_v1_full_research_with_real_results(self, owui, litellm, mcp_tool_defs):
        """
        Full research loop must return ACTUAL CONTENT — not XML, not just tool calls.

        The model must: call searxng_search → receive real results → synthesize a text answer
        with specific, real information including actual laptop brand names.
        """
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        tools = _select_tools(mcp_tool_defs, {"searxng_search", "searxng_read_url"})
        messages = [
            {
                "role": "system",
                "content": (
                    f"Today's date is {today} (UTC).\n"
                    "You are a helpful research assistant with access to web search tools. "
                    "Use tools to search for current information, then write a comprehensive answer. "
                    "Stop calling tools after 3-5 searches and write your final answer."
                ),
            },
            {
                "role": "user",
                "content": "research what are some good laptops to use for elderly parents",
            },
        ]

        final, called, had_xml = _run_tool_loop(owui, litellm, messages, tools, max_tool_turns=8, model=V1_CUSTOM_MODEL)

        assert not had_xml, (
            "murderbot-v1 produced <function=...> XML — native tool calling broken."
        )
        assert any(n in ("searxng_search", "searxng_read_url") for n in called), (
            f"No web search tool was called. Tools called: {called}"
        )
        assert len(final) > 200, (
            f"Answer too short ({len(final)} chars) — model did not synthesize real results.\n"
            f"Answer: {final!r}\nTools: {called}"
        )
        real_content_markers = ["lenovo", "dell", "hp", "asus", "acer", "apple", "samsung",
                                 "chromebook", "macbook", "thinkpad", "inspiron", "laptop"]
        content_lower = final.lower()
        assert any(m in content_lower for m in real_content_markers), (
            f"Answer does not mention any real laptop brands — model did not use search results.\n"
            f"Answer: {final[:500]!r}\nTools: {called}"
        )
