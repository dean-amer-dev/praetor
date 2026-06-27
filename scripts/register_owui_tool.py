#!/usr/bin/env python3
"""
Bootstrap OpenWebUI for Praetor tool calling.

Idempotent — safe to run after any OWU reset or redeploy.

Run:
  OWUI_BASE_URL=https://bot.amer.dev OWUI_ADMIN_EMAIL=alex@amer.dev \
  OWUI_ADMIN_PASSWORD=<password> python scripts/register_owui_tool.py

What it does:
  1. Ensures the praetor_dispatch Python tool exists (dispatch_task + get_task_status only;
     web_search/web_read_url come from the LiteLLM MCP Gateway tool, not here)
  2. Ensures the date_injector global Filter exists — prepends "Today is <date>" to every
     system prompt so the model can do date-accurate searches
  3. Ensures the qwen3-35b-think-custom model exists with:
       - function_calling=native
       - toolIds: ["praetor_dispatch", "server:mcp:lm"]
       - minimal behavioral system prompt (date line comes from the filter)
  4. server:mcp:lm (LiteLLM MCP Gateway) is registered by OWU's MCP server config, not here.
     This script just ensures the model's toolIds reference it.

LiteLLM MCP tools exposed via server:mcp:lm (as of 2026-06-25):
  web_search, web_read_url,
  infra_scaffold, infra_provision, infra_deploy_pr, infra_add_runner,
  infra_check_secrets, infra_app_status, infra_resolve_secret,
  github_ls, github_read, github_search, github_prs, github_pr_diff,
  github_commits, github_tree,
  praetor_dispatch, praetor_create_agent, praetor_create_app, praetor_add_mcp,
  praetor_status, praetor_memory_search, praetor_execute_spec
"""
from __future__ import annotations

import os
import sys

import httpx

OWUI_BASE_URL = os.environ.get("OWUI_BASE_URL", "https://bot.amer.dev").rstrip("/")
OWUI_ADMIN_EMAIL = os.environ.get("OWUI_ADMIN_EMAIL", "alex@amer.dev")
OWUI_ADMIN_PASSWORD = os.environ.get("OWUI_ADMIN_PASSWORD", "")

# ---------------------------------------------------------------------------
# praetor_dispatch Python tool — Praetor task dispatch only.
# web_search / web_read_url intentionally omitted: those come from server:mcp:lm.
# ---------------------------------------------------------------------------
TOOL_ID = "praetor_dispatch"
TOOL_NAME = "Praetor Dispatch"
TOOL_DESCRIPTION = "Dispatch Praetor agent tasks (code, pipeline) and check their status."

TOOL_CONTENT = '''\
"""Praetor Agent Dispatch"""
import os
import httpx
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        PRAETOR_BASE_URL: str = "https://praetor.amer.dev"
        # env var wins when set (after Komodo redeploy); hardcoded key is fallback for now
        PRAETOR_API_KEY: str = Field(
            default_factory=lambda: os.environ.get("PRAETOR_API_KEY", "dRykVJyZp79Ute6JRKlZAgTuMs2jMXodKpszRyj-8aY")
        )

    def __init__(self):
        self.valves = self.Valves()

    def dispatch_task(self, title: str, description: str, task_type: str) -> str:
        """
        Dispatch a background agent task.
        task_type: openhands | code | pipeline | research
        - openhands: autonomous coding agent — use for ALL code tasks (implement, fix, modify files, open PRs)
        - code: lighter code tasks via Praetor coder
        - pipeline: data pipeline tasks
        - research: deep multi-step autonomous research (ONLY when user says "deep research" or "research task" — NOT for simple questions, use web_search for those)
        Include \'repo: owner/name\' in description for code tasks.
        Returns task_id and confirmation.
        """
        resp = httpx.post(
            f"{self.valves.PRAETOR_BASE_URL}/api/v1/dispatch",
            json={"title": title, "description": description, "type": task_type},
            headers={"Authorization": f"Bearer {self.valves.PRAETOR_API_KEY}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return f"Task {data[\'task_id\']} dispatched ({data[\'event\']}). Check status in a few minutes."

    def get_task_status(self, task_id: int) -> str:
        """Check the status of a previously dispatched Praetor task."""
        resp = httpx.get(
            f"{self.valves.PRAETOR_BASE_URL}/api/v1/status/{task_id}",
            headers={"Authorization": f"Bearer {self.valves.PRAETOR_API_KEY}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data["done"]:
            return f"Done. {data[\'mem0_summary\']}"
        return "Still running. Check back shortly."
'''

# ---------------------------------------------------------------------------
# date_injector Filter — global inlet, prepends current date to system prompt
# ---------------------------------------------------------------------------
FILTER_ID = "date_injector"
FILTER_NAME = "Date Injector"
FILTER_DESCRIPTION = "Prepends today's UTC date to the system prompt on every request."

FILTER_CONTENT = '''\
"""Inject current UTC date into every system prompt."""
from datetime import datetime, timezone
from typing import Optional


class Filter:
    def inlet(self, body: dict, __user: Optional[dict] = None) -> dict:
        date_line = f"Today\'s date is {datetime.now(timezone.utc).strftime(\'%Y-%m-%d\')} (UTC).\\n"
        messages = body.get("messages", [])
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = date_line + messages[0]["content"]
        else:
            messages.insert(0, {"role": "system", "content": date_line})
        body["messages"] = messages
        return body
'''

# ---------------------------------------------------------------------------
# murderbot-v0 model config
# ---------------------------------------------------------------------------
CUSTOM_MODEL_ID = "qwen3-35b-think-custom"
CUSTOM_MODEL_NAME = "murderbot-v0"
BASE_MODEL_ID = "qwen3-35b-think"

SYSTEM_PROMPT = """\
You are a helpful personal assistant with access to web search, GitHub, infrastructure, \
Praetor agent dispatch, and agent factory tools.

## Research / information questions
Use web_search + web_read_url. Do NOT dispatch anything.
Examples: "research X", "look up X", "what is X", "find info on X", "how do I X".
HARD LIMIT: After 6 tool calls total, you MUST stop calling tools and write your answer. \
Do not call another tool after 6. Write the answer with what you have.
Never re-fetch a URL already read in this conversation.

## Deep / autonomous research (multi-step, takes minutes)
ONLY when the user explicitly says "deep research", "research task", or "run a research agent":
call dispatch_task with task_type="research". Describe the research objective in detail.
Do NOT use task_type="research" for anything else.

## Coding tasks (implement, fix bugs, modify files, open PRs)
Call dispatch_task with task_type="openhands". Include "repo: owner/name" in description. \
Write a complete self-contained spec. Do NOT write code yourself.

## Creating a new Praetor agent
Call lm_praetor_create_agent with name (kebab-case), description, event (e.g. "agent:grafana-monitor"), \
and tools list. Present a plan and get approval first. Blocks ~5 min until the agent is live.

## Other dispatch types
- task_type="pipeline" — data pipeline tasks (only if user asks)
- task_type="code" — lighter code tasks via Praetor coder"""


def login(client: httpx.Client) -> str:
    resp = client.post(
        "/api/v1/auths/signin",
        json={"email": OWUI_ADMIN_EMAIL, "password": OWUI_ADMIN_PASSWORD},
    )
    resp.raise_for_status()
    return resp.json()["token"]


def ensure_tool(client: httpx.Client) -> None:
    tools = client.get("/api/v1/tools/").raise_for_status().json()
    existing = next((t for t in tools if t.get("id") == TOOL_ID), None)
    payload = {
        "id": TOOL_ID,
        "name": TOOL_NAME,
        "description": TOOL_DESCRIPTION,
        "content": TOOL_CONTENT,
        "meta": {"description": TOOL_DESCRIPTION},
    }
    if existing is None:
        client.post("/api/v1/tools/create", json=payload).raise_for_status()
        print(f"Created tool '{TOOL_NAME}'")
    else:
        client.post(f"/api/v1/tools/id/{TOOL_ID}/update", json=payload).raise_for_status()
        print(f"Updated tool '{TOOL_NAME}'")


def ensure_filter(client: httpx.Client) -> None:
    funcs = client.get("/api/v1/functions/").raise_for_status().json()
    existing = next((f for f in funcs if f.get("id") == FILTER_ID), None)
    payload = {
        "id": FILTER_ID,
        "name": FILTER_NAME,
        "type": "filter",
        "content": FILTER_CONTENT,
        "meta": {"description": FILTER_DESCRIPTION, "manifest": {}},
    }
    if existing is None:
        client.post("/api/v1/functions/create", json=payload).raise_for_status()
        # OWU create endpoint ignores is_active/is_global — toggle separately
        client.post(f"/api/v1/functions/id/{FILTER_ID}/toggle").raise_for_status()
        client.post(f"/api/v1/functions/id/{FILTER_ID}/toggle/global").raise_for_status()
        print(f"Created filter '{FILTER_NAME}' (active, global)")
    else:
        client.post(f"/api/v1/functions/id/{FILTER_ID}/update", json=payload).raise_for_status()
        # Ensure active + global regardless of previous state
        f = existing
        if not f.get("is_active"):
            client.post(f"/api/v1/functions/id/{FILTER_ID}/toggle").raise_for_status()
        if not f.get("is_global"):
            client.post(f"/api/v1/functions/id/{FILTER_ID}/toggle/global").raise_for_status()
        print(f"Updated filter '{FILTER_NAME}'")


def ensure_custom_model(client: httpx.Client) -> None:
    # Use the direct model endpoint — /api/v1/models/base only returns LiteLLM base models,
    # not OWU custom models, causing false "not found" → failed create on restart.
    resp = client.get(f"/api/v1/models/model?id={CUSTOM_MODEL_ID}")
    existing = resp.json() if resp.status_code == 200 else None

    model_payload = {
        "id": CUSTOM_MODEL_ID,
        "name": CUSTOM_MODEL_NAME,
        "base_model_id": BASE_MODEL_ID,
        "params": {"function_calling": "native"},
        "meta": {
            "profile_image_url": "",
            "description": "murderbot qwen3-35b — personal assistant with tool calling",
            "capabilities": {
                "vision": False, "usage": False, "citations": False,
                "memory": False, "builtin_tools": True,
            },
            "builtinTools": {
                "chats": False, "calendar": False, "tasks": False, "memory": False,
                "notes": False, "channels": False, "web_search": False,
                "automations": False, "image_generation": False,
                "code_interpreter": False, "time": False, "knowledge": False,
            },
            # server:mcp:lm provides: web_search, web_read_url, infra_*, github_*
            # praetor_dispatch provides: dispatch_task, get_task_status
            "toolIds": ["praetor_dispatch", "server:mcp:lm"],
            "system": SYSTEM_PROMPT,
        },
        "is_active": True,
        "access_grants": [],
    }

    if existing is None:
        client.post("/api/v1/models/create", json=model_payload).raise_for_status()
        print(f"Created custom model '{CUSTOM_MODEL_ID}'")
    else:
        client.post("/api/v1/models/model/update", json=model_payload).raise_for_status()
        print(f"Custom model '{CUSTOM_MODEL_ID}' updated (toolIds now include server:mcp:lm)")


def main() -> None:
    if not OWUI_ADMIN_PASSWORD:
        print("Error: OWUI_ADMIN_PASSWORD not set", file=sys.stderr)
        sys.exit(1)

    with httpx.Client(base_url=OWUI_BASE_URL, timeout=15) as client:
        token = login(client)
        client.headers["Authorization"] = f"Bearer {token}"
        ensure_tool(client)
        ensure_filter(client)
        ensure_custom_model(client)
        # NOTE: deactivate_base_model was removed — deactivating qwen3-35b-think breaks
        # custom model routing in OWU 0.9.6 (custom models route through their base_model_id,
        # which OWU requires to be active in the model list)
        print("Done.")


if __name__ == "__main__":
    main()
