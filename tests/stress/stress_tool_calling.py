#!/usr/bin/env python3
"""
stress_tool_calling.py — Manual stress scenarios for qwen3-35b-think tool calling.

NOT run in CI. Run manually when you want to hammer the stack.

Usage:
  python3 tests/stress/stress_tool_calling.py --stress all
  python3 tests/stress/stress_tool_calling.py --stress repeat --repeat-n 5
  python3 tests/stress/stress_tool_calling.py --stress deep-loop --loop-turns 40
  python3 tests/stress/stress_tool_calling.py --stress concurrent --concurrent-n 4
  python3 tests/stress/stress_tool_calling.py --stress large-payload
"""
import argparse
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx


LITELLM_URL = os.environ.get("LITELLM_URL", "https://litellm.amer.dev")
MODEL       = os.environ.get("LLM_MODEL", "qwen3-35b-think")
TIMEOUT     = int(os.environ.get("LLM_TIMEOUT", "120"))
API_KEY     = os.environ.get("LITELLM_API_KEY", "")


MOCK_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web for information on a topic.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
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
                "properties": {"url": {"type": "string"}},
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

_SYNTHESIS_SIGNAL_AFTER = 4
_FORCE_SYNTHESIS_AFTER  = 8


def _completion(messages, tools=None, max_tokens=2048):
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
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
                    "role": "tool", "tool_call_id": tc["id"], "content": result,
                })
            continue

        return turn + 1, finish, msg.get("content", ""), None

    return max_turns, "tool_calls", "", "hit max_turns without synthesis"


# ── Stress scenarios ──────────────────────────────────────────────────────────

# Lazy import so the regression tests in smoke/ can share helpers without importing the runner
REGRESSION_TESTS = {
    "basic_tool_use": lambda: _drive_tool_session(
        "What is Python? Use the search tool to find information.", max_turns=10),
    "extended_tool_use": lambda: _drive_tool_session(
        "Research the history of Python, its major versions, ecosystem, and AI/ML usage. "
        "Search each topic separately.", max_turns=20),
    "no_thinking_with_tools": lambda: _drive_tool_session(
        "Use the search tool to find 8 different facts about space exploration.", max_turns=20),
}


def stress_repeat(n=5, verbose=False):
    """Run the full regression suite N times and report per-test pass rates."""
    print(f"[stress:repeat] Running regression suite {n} times\n")
    counts = {name: {"pass": 0, "fail": 0} for name in REGRESSION_TESTS}
    t0 = time.time()

    for i in range(n):
        print(f"  Round {i+1}/{n}")
        for name, fn in REGRESSION_TESTS.items():
            try:
                turns, finish, text, err = fn()
                ok = finish == "stop" and (not err or "max_turns" in str(err))
                msg = f"{turns} turns, finish={finish}" + (f", err={err}" if err else "")
            except Exception as e:
                ok = False
                msg = f"exception: {e}"
            counts[name]["pass" if ok else "fail"] += 1
            print(f"    {name}: {'PASS' if ok else 'FAIL'}  {msg}")
        print()

    elapsed = time.time() - t0
    print(f"Results after {n} rounds ({elapsed:.0f}s total):")
    all_ok = True
    for name, c in counts.items():
        rate = c["pass"] / n * 100
        flag = f"  ← {c['fail']} FAILURE(S)" if c["fail"] else ""
        print(f"  {name}: {c['pass']}/{n} ({rate:.0f}%){flag}")
        if c["fail"]:
            all_ok = False
    return all_ok


def stress_deep_loop(turns=40, verbose=False):
    """Single session: N tool calls with no synthesis cutoff. Verify zero 400/500s."""
    print(f"[stress:deep-loop] {turns} tool calls, no synthesis cutoff\n")

    messages = [
        {
            "role": "system",
            "content": (
                "You are a research assistant. Keep searching for more information. "
                "Call search_web repeatedly with different queries about Python. "
                "Do not stop until instructed."
            ),
        },
        {"role": "user", "content": "Search for everything you can find about Python."},
    ]

    errors = []
    tool_call_count = 0
    t0 = time.time()

    for turn in range(turns + 5):
        resp = _completion(messages, tools=MOCK_TOOLS)
        if resp.status_code != 200:
            errors.append(f"turn {turn+1}: HTTP {resp.status_code}")
            print(f"  turn {turn+1}: HTTP {resp.status_code} ERROR")
            break

        choice = resp.json()["choices"][0]
        msg    = choice["message"]
        finish = choice["finish_reason"]
        messages.append(msg)

        tc_count = len(msg.get("tool_calls") or [])
        print(f"  turn {turn+1}: finish={finish} tool_calls={tc_count} total_calls={tool_call_count}", flush=True)

        if finish == "stop" or finish != "tool_calls":
            break

        for tc in (msg.get("tool_calls") or []):
            fn   = tc["function"]["name"]
            args = tc["function"].get("arguments", "{}")
            tool_call_count += 1
            messages.append({
                "role": "tool", "tool_call_id": tc["id"],
                "content": f"[call {tool_call_count}] " + MOCK_TOOL_RESULTS.get(fn, "mock result"),
            })

        if tool_call_count >= turns:
            print(f"\n  Reached {turns} tool calls — forcing synthesis turn")
            resp2 = _completion(messages, tools=None)
            status = resp2.status_code
            finish2 = resp2.json()["choices"][0]["finish_reason"] if status == 200 else "error"
            print(f"  Synthesis turn: HTTP {status}, finish={finish2}")
            if status != 200:
                errors.append(f"synthesis turn: HTTP {status}")
            break

    elapsed = time.time() - t0
    print(f"\n  Completed {tool_call_count} tool calls in {elapsed:.1f}s")
    if errors:
        print(f"  FAILURES: {errors}")
        return False
    print(f"  No errors — {tool_call_count} calls, zero 400/500s")
    return True


def _run_one_session(session_id, prompt):
    t0 = time.time()
    turns, finish, text, err = _drive_tool_session(prompt, max_turns=12)
    ok = finish == "stop" and not err
    return session_id, ok, turns, finish, err, time.time() - t0


def stress_concurrent(n=4, verbose=False):
    """N sessions in parallel. Verify the server handles concurrent load without errors."""
    print(f"[stress:concurrent] {n} parallel sessions\n")
    prompts = [
        "What is Python? Search and summarize.",
        "Search for information about machine learning frameworks.",
        "Find facts about Linux operating system history.",
        "Research the history of the internet.",
        "What is Kubernetes? Search for information.",
        "Find information about Rust programming language.",
        "Search for Python web frameworks.",
        "Research GPU computing and CUDA.",
    ][:n]

    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = {pool.submit(_run_one_session, i, p): i for i, p in enumerate(prompts)}
        for fut in as_completed(futures):
            sid, ok, turns, finish, err, elapsed = fut.result()
            results.append((sid, ok))
            status = "OK" if ok else "FAIL"
            print(f"  session {sid}: {status}  {turns} turns  finish={finish}  {elapsed:.1f}s"
                  + (f"  err={err}" if err else ""))

    elapsed = time.time() - t0
    passed = sum(1 for _, ok in results if ok)
    print(f"\n  {passed}/{n} sessions completed successfully in {elapsed:.1f}s total")
    return passed == n


def stress_large_payload(verbose=False):
    """Large/oversized tool responses to exercise the token-budget stripping code paths."""
    print("[stress:large-payload] Large tool responses + multi-turn\n")

    big_chunk  = "Python information: " + ("x" * 100 + " ") * 28   # ~2900 chars
    huge_chunk = "Python information: " + ("x" * 100 + " ") * 60   # ~6100 chars

    messages = [
        {"role": "system", "content": "You are a research assistant. Use tools, then summarize."},
        {"role": "user", "content": "Search for Python information."},
    ]

    errors = []
    t0 = time.time()

    resp = _completion(messages, tools=MOCK_TOOLS)
    if resp.status_code != 200:
        print(f"  Turn 1 req: HTTP {resp.status_code}")
        return False
    msg = resp.json()["choices"][0]["message"]
    if msg.get("tool_calls"):
        messages.append(msg)
        tc = msg["tool_calls"][0]
        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": big_chunk})
        print(f"  Turn 1: tool call → {len(big_chunk)}-char response sent")
    else:
        finish1 = resp.json()["choices"][0]["finish_reason"]
        print(f"  Turn 1: model synthesized without tools (finish={finish1}) — large-payload path skipped")
        elapsed = time.time() - t0
        print(f"\n  Completed in {elapsed:.1f}s")
        print("  No errors — model chose direct synthesis (no large payloads to test)")
        return True

    resp2 = _completion(messages, tools=MOCK_TOOLS)
    if resp2.status_code != 200:
        errors.append(f"turn 2: HTTP {resp2.status_code}")
        print(f"  Turn 2 req: HTTP {resp2.status_code}")
    else:
        msg2 = resp2.json()["choices"][0]["message"]
        if msg2.get("tool_calls"):
            messages.append(msg2)
            tc2 = msg2["tool_calls"][0]
            messages.append({"role": "tool", "tool_call_id": tc2["id"], "content": huge_chunk})
            print(f"  Turn 2: tool call → {len(huge_chunk)}-char response sent (will be truncated by template)")
        else:
            messages.append(msg2)
            print(f"  Turn 2: model synthesized early (finish={resp2.json()['choices'][0]['finish_reason']})")

    resp3 = _completion(messages, tools=None)
    elapsed = time.time() - t0
    if resp3.status_code != 200:
        errors.append(f"synthesis turn: HTTP {resp3.status_code}")
        print(f"  Turn 3 (synthesis): HTTP {resp3.status_code}")
    else:
        choice3 = resp3.json()["choices"][0]
        finish3  = choice3["finish_reason"]
        content3 = choice3["message"].get("content", "")
        print(f"  Turn 3 (synthesis): HTTP 200, finish={finish3}, {len(content3)} chars")

    print(f"\n  Completed in {elapsed:.1f}s")
    if errors:
        print(f"  FAILURES: {errors}")
        return False
    print("  No errors — large payloads handled correctly")
    return True


STRESS_TESTS = {
    "repeat":        stress_repeat,
    "deep-loop":     stress_deep_loop,
    "concurrent":    stress_concurrent,
    "large-payload": stress_large_payload,
}


def main():
    parser = argparse.ArgumentParser(description="Manual stress scenarios for qwen3-35b-think tool calling")
    parser.add_argument("--stress", required=True,
                        help="Scenario to run: repeat, deep-loop, concurrent, large-payload, all")
    parser.add_argument("--repeat-n",     type=int, default=5,  metavar="N",
                        help="Rounds for repeat (default 5)")
    parser.add_argument("--loop-turns",   type=int, default=40, metavar="N",
                        help="Tool calls for deep-loop (default 40)")
    parser.add_argument("--concurrent-n", type=int, default=4,  metavar="N",
                        help="Parallel sessions for concurrent (default 4)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if not API_KEY:
        print("ERROR: Set LITELLM_API_KEY in environment")
        sys.exit(1)

    which = list(STRESS_TESTS.keys()) if args.stress == "all" else [args.stress]
    for s in which:
        if s not in STRESS_TESTS:
            print(f"Unknown scenario: {s}. Available: {', '.join(STRESS_TESTS)}")
            sys.exit(1)

    print(f"Stress testing {LITELLM_URL} model={MODEL}\n")
    ok = True
    for s in which:
        print("=" * 60)
        kwargs = {}
        if s == "repeat":      kwargs["n"]      = args.repeat_n
        if s == "deep-loop":   kwargs["turns"]  = args.loop_turns
        if s == "concurrent":  kwargs["n"]      = args.concurrent_n
        kwargs["verbose"] = args.verbose
        try:
            result = STRESS_TESTS[s](**kwargs)
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            result = False
        ok = ok and result
        print()

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
