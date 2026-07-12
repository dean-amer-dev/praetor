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

CUSTOM_MODEL  = "murderbot-v1-custom"
BASE_MODEL    = "murderbot-v1-base"

SECURE_SEARCH_MCP_URL = os.environ.get("SECURE_SEARCH_MCP_URL", "https://secure-search-mcp.amer.dev")

# All active OWU custom models: (custom_model_id, display_name, base_model_id)
ALL_CUSTOM_MODELS = [
    ("murderbot-v1-custom",            "murderbot-v1",            "murderbot-v1-base"),
    ("murderbot-uncensored-v1-custom", "murderbot-uncensored-v1", "murderbot-uncensored-v1-base"),
    ("archlinux-v0-custom",            "archlinux-v0",            "archlinux-v0-base"),
    ("archlinux-uncensored-v0-custom", "archlinux-uncensored-v0", "archlinux-uncensored-v0-base"),
]

# Secure-only models (toolIds: ["secure_search"] only)
SECURE_ONLY_MODELS = [
    ("murderbot-v1-secure-custom",  "murderbot-v1-secure",  "murderbot-v1-base"),
    ("archlinux-v0-secure-custom",  "archlinux-v0-secure",  "archlinux-v0-base"),
]

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
        assert resp.json().get("name") == "murderbot-v1"

    def test_all_custom_models_exist(self, owui):
        """All 4 active OWU custom models must exist with correct display name and base_model_id."""
        for custom_id, display_name, base_id in ALL_CUSTOM_MODELS:
            resp = owui.get(f"/api/v1/models/model?id={custom_id}")
            assert resp.status_code == 200, (
                f"Custom model {custom_id!r} not found (HTTP {resp.status_code}). "
                "Run scripts/register_owui_tool.py to restore it."
            )
            data = resp.json()
            assert data.get("name") == display_name, (
                f"{custom_id}: expected name={display_name!r}, got {data.get('name')!r}"
            )
            assert data.get("base_model_id") == base_id, (
                f"{custom_id}: expected base_model_id={base_id!r}, got {data.get('base_model_id')!r}"
            )

    def test_function_calling_native(self, owui):
        """function_calling must be 'native' on all models — otherwise OWU outputs XML it cannot execute."""
        for custom_id, display_name, _ in ALL_CUSTOM_MODELS:
            resp = owui.get(f"/api/v1/models/model?id={custom_id}")
            assert resp.status_code == 200
            fc = resp.json().get("params", {}).get("function_calling")
            assert fc == "native", (
                f"{custom_id} function_calling={fc!r}. Must be 'native' — text injection produces "
                "<function=...> XML that OWU's parser ignores, silently breaking all tool calls."
            )

    def test_praetor_dispatch_in_tool_ids(self, owui):
        resp = owui.get(f"/api/v1/models/model?id={CUSTOM_MODEL}")
        assert resp.status_code == 200
        tool_ids = resp.json().get("meta", {}).get("toolIds", [])
        assert "praetor_dispatch" in tool_ids, f"praetor_dispatch missing from toolIds: {tool_ids}"

    def test_server_mcp_lm_in_tool_ids(self, owui):
        """server:mcp:lm must be in toolIds on all models — provides lm_searxng_search etc."""
        for custom_id, _, _ in ALL_CUSTOM_MODELS:
            resp = owui.get(f"/api/v1/models/model?id={custom_id}")
            assert resp.status_code == 200
            tool_ids = resp.json().get("meta", {}).get("toolIds", [])
            assert "server:mcp:lm" in tool_ids, (
                f"server:mcp:lm missing from {custom_id} toolIds: {tool_ids}. "
                "Run scripts/register_owui_tool.py."
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
        """All active models must have a system prompt."""
        for custom_id, _, _ in ALL_CUSTOM_MODELS:
            resp = owui.get(f"/api/v1/models/model?id={custom_id}")
            assert resp.status_code == 200
            system = resp.json().get("meta", {}).get("system", "")
            assert len(system) > 50, (
                f"{custom_id}: system prompt missing or too short ({len(system)} chars). "
                "Run scripts/register_owui_tool.py."
            )

    def test_secure_search_tool_registered(self, owui):
        """secure_search Python tool must exist with secure_search() and secure_read_url() methods."""
        resp = owui.get("/api/v1/tools/")
        assert resp.status_code == 200
        t = next((x for x in resp.json() if x.get("id") == "secure_search"), None)
        assert t is not None, (
            "secure_search tool not found in OWU. Run scripts/register_owui_tool.py."
        )
        content = t.get("content", "")
        assert "secure_search" in content, "secure_search() method missing from tool"
        assert "secure_read_url" in content, "secure_read_url() method missing from tool"
        assert "secure-search-mcp.amer.dev" in content, "secure-search-mcp URL missing from tool"

    def test_secure_search_in_all_model_tool_ids(self, owui):
        """secure_search must be in toolIds on all regular models (but NOT on secure-only models)."""
        for custom_id, _, _ in ALL_CUSTOM_MODELS:
            resp = owui.get(f"/api/v1/models/model?id={custom_id}")
            assert resp.status_code == 200
            tool_ids = resp.json().get("meta", {}).get("toolIds", [])
            assert "secure_search" in tool_ids, (
                f"secure_search missing from {custom_id} toolIds: {tool_ids}. "
                "Run scripts/register_owui_tool.py."
            )

    def test_secure_only_models_exist(self, owui):
        """Secure-only model variants must exist with toolIds=['secure_search'] only."""
        for custom_id, display_name, base_id in SECURE_ONLY_MODELS:
            resp = owui.get(f"/api/v1/models/model?id={custom_id}")
            assert resp.status_code == 200, (
                f"Secure-only model {custom_id!r} not found (HTTP {resp.status_code}). "
                "Run scripts/register_owui_tool.py."
            )
            data = resp.json()
            assert data.get("name") == display_name, (
                f"{custom_id}: expected name={display_name!r}, got {data.get('name')!r}"
            )
            tool_ids = data.get("meta", {}).get("toolIds", [])
            assert tool_ids == ["secure_search"], (
                f"{custom_id} toolIds={tool_ids!r} — should be ['secure_search'] only. "
                "Secure-only models must not have server:mcp:lm or praetor_dispatch."
            )

    def test_date_injector_filter_active_and_global(self, owui):
        """date_injector filter must be active and global — prepends today's date to every system prompt."""
        resp = owui.get("/api/v1/functions/")
        assert resp.status_code == 200
        f = next((x for x in resp.json() if x.get("id") == "date_injector"), None)
        assert f is not None, "date_injector filter not found. Run scripts/register_owui_tool.py."
        assert f.get("is_active") is True, f"date_injector is disabled (is_active={f.get('is_active')})"
        assert f.get("is_global") is True, f"date_injector is not global (is_global={f.get('is_global')})"

    def test_base_model_visible(self, owui):
        """murderbot-v1-base must be visible in OWU's model list (fetched from LiteLLM)."""
        resp = owui.get("/api/v1/models")
        assert resp.status_code == 200, f"OWU /api/v1/models HTTP {resp.status_code}"
        models = resp.json() if isinstance(resp.json(), list) else resp.json().get("data", [])
        model_ids = [m.get("id") for m in models]
        assert BASE_MODEL in model_ids, (
            f"{BASE_MODEL!r} not visible in OWU. LiteLLM must serve it and OWU must connect. "
            f"Present models: {[m for m in model_ids if 'murderbot' in str(m)]}"
        )

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
                "max_tokens": 2048,  # thinking model needs room: ~500-1000 thinking tokens + tool call JSON
            },
            timeout=300,
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
            timeout=300,  # thinking model queues behind other requests; wait up to 5 min
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

    def test_litellm_mcp_exposes_secure_search_tools(self, mcp_tool_defs):
        """secure_search MCP server must be registered in LiteLLM and expose tools."""
        names = {t["name"] for t in mcp_tool_defs}
        # LiteLLM namespaces tools from the "secure_search" MCP server with "secure_search_" prefix
        secure_tools = {n for n in names if n.startswith("secure_search_")}
        assert len(secure_tools) > 0, (
            f"No secure_search_* tools in LiteLLM MCP. Got: {sorted(names)}. "
            "Check apps/litellm/server/configmap.yaml — secure_search MCP server must be listed."
        )
        assert "secure_search_searxng_search" in names or any("search" in n for n in secure_tools), (
            f"Expected secure_search_searxng_search in MCP tools. Got secure tools: {secure_tools}"
        )

    def test_litellm_serves_all_base_models(self, litellm):
        """All 4 base models must be served by LiteLLM — OWU custom models route through these."""
        resp = litellm.get("/v1/models", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        ids = {m["id"] for m in resp.json()["data"]}
        for _, _, base_id in ALL_CUSTOM_MODELS:
            assert base_id in ids, (
                f"{base_id!r} not in LiteLLM models: {sorted(ids)}. "
                "Check apps/litellm/server/configmap.yaml in k3s-dean-gitops."
            )

    def test_litellm_native_tool_call_baseline(self, litellm, mcp_tool_defs):
        """Direct LiteLLM call must return tool_calls JSON. If this fails, vLLM tool calling is broken."""
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


# ── Secure Search ─────────────────────────────────────────────────────────────

class TestSecureSearch:
    """
    Tests for the secure_search tool category — NordVPN Switzerland VPN-protected search.

    Tests: MCP server health, VPN gate active, tool execution via Python tool,
    secure-only model routing, integration with LiteLLM MCP namespace.
    """

    def test_secure_search_mcp_health(self):
        """secure-search-mcp /health endpoint must return 200."""
        resp = httpx.get(f"{SECURE_SEARCH_MCP_URL}/health", timeout=15)
        assert resp.status_code == 200, (
            f"secure-search-mcp /health returned HTTP {resp.status_code}: {resp.text[:200]}. "
            "Check if gluetun VPN sidecar is connected and MCP server is running."
        )

    def test_secure_search_mcp_tools_list(self):
        """secure-search-mcp must expose searxng_search and searxng_read_url tools."""
        resp = httpx.post(
            f"{SECURE_SEARCH_MCP_URL}/mcp",
            content=b'{"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}',
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        assert resp.status_code == 200, f"tools/list HTTP {resp.status_code}: {resp.text[:200]}"
        tool_names: set[str] = set()
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                    tools = payload.get("result", {}).get("tools", [])
                    tool_names.update(t["name"] for t in tools)
                except Exception:
                    pass
        if not tool_names:
            try:
                payload = json.loads(resp.text)
                tools = payload.get("result", {}).get("tools", [])
                tool_names.update(t["name"] for t in tools)
            except Exception:
                pass
        assert "searxng_search" in tool_names, (
            f"searxng_search not in secure-search-mcp tools: {tool_names}"
        )
        assert "searxng_read_url" in tool_names, (
            f"searxng_read_url not in secure-search-mcp tools: {tool_names}"
        )

    def test_secure_search_returns_results(self):
        """secure_search must execute a real search and return non-empty results via VPN."""
        resp = httpx.post(
            f"{SECURE_SEARCH_MCP_URL}/mcp",
            content=json.dumps({
                "jsonrpc": "2.0",
                "id": "smoke",
                "method": "tools/call",
                "params": {
                    "name": "searxng_search",
                    "arguments": {"query": "Python programming language", "max_results": 3},
                },
            }).encode(),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        assert resp.status_code == 200, (
            f"secure_search tools/call HTTP {resp.status_code}: {resp.text[:300]}. "
            "If 503: gluetun VPN may not be connected. Check NordVPN credentials in BWS."
        )
        result_text = ""
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                    content_list = payload.get("result", {}).get("content", [])
                    result_text = " ".join(c.get("text", "") for c in content_list if c.get("type") == "text")
                    if result_text:
                        break
                except Exception:
                    pass
        if not result_text:
            try:
                payload = json.loads(resp.text)
                content_list = payload.get("result", {}).get("content", [])
                result_text = " ".join(c.get("text", "") for c in content_list if c.get("type") == "text")
            except Exception:
                pass

        assert len(result_text) > 50, (
            f"secure_search returned no content ({len(result_text)} chars): {result_text!r}. "
            "VPN may be up but SearXNG may be unreachable from the VPN egress IP."
        )
        assert "python" in result_text.lower(), (
            f"secure_search results don't mention 'python' for a python query: {result_text[:300]!r}"
        )

    def test_secure_only_model_tool_ids_enforced(self, owui):
        """Secure-only models must have exactly ['secure_search'] in toolIds — nothing else."""
        for custom_id, display_name, _ in SECURE_ONLY_MODELS:
            resp = owui.get(f"/api/v1/models/model?id={custom_id}")
            if resp.status_code != 200:
                pytest.skip(f"{custom_id} not found — run scripts/register_owui_tool.py")
            tool_ids = resp.json().get("meta", {}).get("toolIds", [])
            assert tool_ids == ["secure_search"], (
                f"{custom_id} toolIds={tool_ids!r} — expected exactly ['secure_search']. "
                "Secure-only models must not expose any tools other than secure_search."
            )

    def test_secure_only_model_tool_call_uses_secure_search(self, owui):
        """
        Secure-only model must call secure_search (not searxng_search) for a research question.

        Uses tool_choice='required' to force a tool call. The only available tool is secure_search
        (from the Python tool), so any tool call must use that.
        """
        SECURE_TOOL_DEF = {
            "type": "function",
            "function": {
                "name": "secure_search",
                "description": "Search the web via NordVPN Switzerland VPN tunnel.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
                    "required": ["query"],
                },
            },
        }
        resp = owui.post(
            "/api/v1/chat/completions",
            json={
                "model": SECURE_ONLY_MODELS[0][0],  # murderbot-v1-secure-custom
                "messages": [
                    {"role": "system", "content": "You are in SECURE RESEARCH MODE. Use only secure_search."},
                    {"role": "user", "content": "Research the latest news about open source AI models."},
                ],
                "tools": [SECURE_TOOL_DEF],
                "tool_choice": "required",
                "stream": False,
                "max_tokens": 256,
            },
        )
        assert resp.status_code == 200, f"OWU HTTP {resp.status_code}: {resp.text[:300]}"
        choice = resp.json()["choices"][0]
        msg = choice["message"]
        tc = msg.get("tool_calls") or []
        content = msg.get("content") or ""
        assert "<function=" not in content, "secure-only model produced XML tool call"
        assert choice["finish_reason"] == "tool_calls", (
            f"finish_reason={choice['finish_reason']!r} — expected tool_calls for secure-only model"
        )
        if tc:
            called = tc[0]["function"]["name"]
            assert called == "secure_search", (
                f"secure-only model called {called!r} instead of secure_search"
            )


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


# ── murderbot-v1 regression tests (Qwen3.6-27B, vLLM, RTX PRO 4000) ──────────

class TestMurderbotV1:
    """
    Regression tests specific to murderbot-v1 (murderbot-v1-custom / murderbot-v1-base).

    These cover synthesis-token-budget exhaustion (the original no-output regression)
    and full research loop with real MCP results.
    """

    def test_v1_thinking_does_not_consume_full_output_budget(self, litellm):
        """
        Regression: when max_output_tokens=2048 in the litellm configmap, OWU sent
        max_tokens=2048 to vLLM. The model exhausted the entire budget on reasoning
        and produced 0 visible characters ("Thought for 2 minutes" then blank).

        Fix: max_output_tokens >= 4096 so the model has room to think (~2000 tokens)
        and still produce visible output.

        This test checks the LiteLLM model_info config directly — a fast, reliable
        assertion that catches the exact misconfiguration without requiring live inference
        (which would need a 5-10 minute timeout on a loaded GPU server).
        """
        resp = litellm.get("/model/info", headers={"Accept": "application/json"})
        assert resp.status_code == 200, f"LiteLLM /model/info HTTP {resp.status_code}: {resp.text[:200]}"
        models = {m["model_name"]: m.get("model_info", {}) for m in resp.json().get("data", [])}
        assert BASE_MODEL in models, (
            f"{BASE_MODEL!r} not in LiteLLM model list. "
            "Check apps/litellm/server/configmap.yaml in k3s-dean-gitops."
        )
        max_output = models[BASE_MODEL].get("max_output_tokens", 0)
        assert max_output >= 4096, (
            f"{BASE_MODEL} max_output_tokens={max_output} is too small.\n"
            "vLLM shares the max_tokens budget between thinking tokens and visible content. "
            "When max_output_tokens < 4096, thinking alone can exhaust the entire budget, "
            "leaving 0 tokens for visible content.\n"
            "Fix: set max_output_tokens >= 4096 (current target: 8192) for "
            f"{BASE_MODEL} in apps/litellm/server/configmap.yaml (k3s-dean-gitops)."
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
                "model": CUSTOM_MODEL,
                "messages": [{"role": "user", "content": "Search for best laptops for elderly people."}],
                "tools": tools,
                "tool_choice": "required",
                "stream": False,
                "max_tokens": 2048,  # thinking model needs room: ~500-1000 thinking + tool call JSON
            },
            timeout=300,
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

    def test_v1_research_produces_synthesis(self, owui, litellm, mcp_tool_defs):
        """
        Research question via murderbot-v1 must produce a substantive text answer.

        This is the test that would have caught the live regression: user sent a
        research question, model called tools, response returned empty content.

        Root cause (fixed): 27B model (16K context limit) + 5+ real tool calls (~14K
        input tokens) + synthesis requesting 2048 output = 16385 > 16384 → HTTP 400.
        Fix: max_completion_tokens: 1024 in LiteLLM config for murderbot-v1-base.
        """
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        RESEARCH_TOOLS = {"searxng_search", "searxng_read_url", "praetor_memory_search"}
        tools = _select_tools(mcp_tool_defs, RESEARCH_TOOLS)
        tools.append(_dispatch_tool_def())

        model_resp = owui.get(f"/api/v1/models/model?id={CUSTOM_MODEL}")
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
            owui, litellm, messages, tools, max_tool_turns=5, model=CUSTOM_MODEL,
        )

        assert not had_xml, (
            f"murderbot-v1 used XML tool syntax — function_calling must be 'native' on {CUSTOM_MODEL}"
        )
        assert len(tool_calls_made) >= 1, (
            "murderbot-v1 never called any tools for a research question — check toolIds config."
        )
        assert len(final_content) > 100, (
            f"murderbot-v1 research synthesis too short ({len(final_content)} chars).\n"
            f"Tools called: {tool_calls_made}\nContent: {final_content!r}\n"
            "If empty or 400: check murderbot-v1-base has max_completion_tokens: 1024 "
            "in apps/litellm/server/configmap.yaml (prevents context window overflow)."
        )

    def test_v1_synthesis_token_budget(self, owui, litellm, mcp_tool_defs):
        """
        Regression: 5 pre-baked tool turns + synthesis at max_tokens=1500 must not 400.

        Root cause: 27B model has 16K total context. 5 realistic tool results push
        input tokens to ~10K. 1500 output tokens at LiteLLM level = fine (10K + 1500 = 11.5K).
        But if LiteLLM config did NOT have truncate_prompt_tokens, a real 5-turn session
        with large results could exceed 14K input → 14K + 1500 = 15.5K, still fine.
        The hard cap is max_completion_tokens: 1024 set in LiteLLM for the base model
        which caps synthesis output server-side regardless of what the client requests.

        Uses pre-baked tool turns (no live MCP calls) so this test runs fast.
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

        resp = owui.post(
            "/api/v1/chat/completions",
            json={
                "model": CUSTOM_MODEL,
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
            f"27B synthesis empty or too short ({len(content)} chars, finish={finish!r}).\n"
            f"Content: {content[:200]!r}\n"
            "Check murderbot-v1-base config in apps/litellm/server/configmap.yaml."
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

        final, called, had_xml = _run_tool_loop(owui, litellm, messages, tools, max_tool_turns=8, model=CUSTOM_MODEL)

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


# ── archlinux-v0 (Qwen3:14B, Ollama, RX 9070 XT) ─────────────────────────────

class TestArchlinuxV0:
    """
    Smoke tests for archlinux-v0 (archlinux-v0-custom / archlinux-v0-base).

    Different hardware path from murderbot: Ollama on AMD RX 9070 XT vs vLLM on
    NVIDIA RTX PRO 4000. Separate test class catches host-specific routing failures.
    """

    ARCHLINUX_CUSTOM = "archlinux-v0-custom"
    ARCHLINUX_BASE   = "archlinux-v0-base"

    def test_archlinux_model_exists(self, owui):
        resp = owui.get(f"/api/v1/models/model?id={self.ARCHLINUX_CUSTOM}")
        assert resp.status_code == 200, (
            f"archlinux-v0 ({self.ARCHLINUX_CUSTOM}) not found: HTTP {resp.status_code}. "
            "Run scripts/register_owui_tool.py to restore it."
        )
        assert resp.json().get("name") == "archlinux-v0"

    def test_archlinux_no_xml_tool_calls(self, owui, mcp_tool_defs):
        """Model must return JSON tool_calls — XML means function_calling is not 'native'."""
        tools = _select_tools(mcp_tool_defs, {"searxng_search"})
        resp = owui.post(
            "/api/v1/chat/completions",
            json={
                "model": self.ARCHLINUX_CUSTOM,
                "messages": [{"role": "user", "content": "Search for recent Python releases."}],
                "tools": tools,
                "tool_choice": "required",
                "stream": False,
                "max_tokens": 300,
            },
        )
        assert resp.status_code == 200, f"OWU HTTP {resp.status_code}: {resp.text[:300]}"
        choice = resp.json()["choices"][0]
        content = choice["message"].get("content") or ""
        assert "<function=" not in content, (
            f"archlinux-v0 produced XML — set function_calling='native' on {self.ARCHLINUX_CUSTOM}"
        )
        assert choice["finish_reason"] == "tool_calls", (
            f"finish_reason={choice['finish_reason']!r}, content={content[:200]!r}"
        )

    def test_archlinux_research_produces_synthesis(self, owui, litellm, mcp_tool_defs):
        """archlinux-v0 must call web search and produce a substantive text answer."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        tools = _select_tools(mcp_tool_defs, {"searxng_search", "searxng_read_url"})
        messages = [
            {
                "role": "system",
                "content": (
                    f"Today's date is {today} (UTC).\n"
                    "You are a helpful research assistant. Use web search tools to gather "
                    "information, then write a concise answer. Stop after 3-4 tool calls."
                ),
            },
            {"role": "user", "content": "What are some popular home automation platforms and their key features?"},
        ]

        final, called, had_xml = _run_tool_loop(
            owui, litellm, messages, tools, max_tool_turns=5, model=self.ARCHLINUX_CUSTOM,
        )

        assert not had_xml, (
            f"archlinux-v0 produced XML — function_calling must be 'native' on {self.ARCHLINUX_CUSTOM}"
        )
        assert any(n in ("searxng_search", "searxng_read_url") for n in called), (
            f"archlinux-v0 did not call any web search tool. Tools called: {called}"
        )
        assert len(final) > 100, (
            f"archlinux-v0 synthesis too short ({len(final)} chars).\n"
            f"Tools: {called}\nContent: {final!r}"
        )
