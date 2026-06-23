#!/usr/bin/env python3
"""
Bootstrap OpenWebUI for Praetor tool calling.

Idempotent — safe to run after any OWU reset or redeploy.

Run:
  OWUI_BASE_URL=https://bot.amer.dev OWUI_ADMIN_EMAIL=alex@amer.dev \
  OWUI_ADMIN_PASSWORD=<password> python scripts/register_owui_tool.py

What it does:
  1. Ensures the praetor_dispatch native tool exists (with env-based PRAETOR_API_KEY)
  2. Ensures the qwen3-35b-think-custom model exists with function_calling=native,
     toolIds, and the OWU system prompt — this model is never touched by LiteLLM sync
  3. Deactivates the raw qwen3-35b-think base model so users only see the custom one
"""
from __future__ import annotations

import json
import os
import sys

import httpx

OWUI_BASE_URL = os.environ.get("OWUI_BASE_URL", "https://bot.amer.dev").rstrip("/")
OWUI_ADMIN_EMAIL = os.environ.get("OWUI_ADMIN_EMAIL", "alex@amer.dev")
OWUI_ADMIN_PASSWORD = os.environ.get("OWUI_ADMIN_PASSWORD", "")

TOOL_ID = "praetor_dispatch"
TOOL_NAME = "Praetor Dispatch"
TOOL_DESCRIPTION = "Dispatch Praetor agent tasks (research, code, pipeline) and check their status."

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
        Dispatch a Praetor agent task.
        task_type: research | code | pipeline | openhands
        For code/pipeline/openhands tasks, include \'repo: owner/name\' in description.
        Use openhands to send fully autonomous coding tasks to the OpenHands agent.
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

CUSTOM_MODEL_ID = "qwen3-35b-think-custom"
CUSTOM_MODEL_NAME = "murderbot-v0"
BASE_MODEL_ID = "qwen3-35b-think"

SYSTEM_PROMPT = """\
You are a helpful personal assistant with access to web search, URL reading, and Praetor agent dispatch tools.

## Coding tasks — use dispatch_task, never write code yourself

When the user asks you to implement code, modify files, add a feature, fix a bug, or make any changes to a codebase or repository:
- Call dispatch_task with task_type="openhands" — this sends the task to an autonomous coding agent
- Do NOT write the code yourself or explain what the code should look like
- Include the target repo in the description (e.g. "repo: amerenda/dean-mcp")
- The description should be a complete, self-contained spec the agent can execute without asking questions

For other agent types:
- task_type="research" — web research tasks
- task_type="code" — lighter code tasks (the Praetor coder agent, not OpenHands)
- task_type="pipeline" — data pipeline tasks

## Research tasks — use web search tools

When doing research:
- Use web_search first to identify relevant pages, then web_read_url to read them
- When reading a URL, call web_read_url with read_headings=True first to get the outline, then use section= to read only the relevant part — never request more than 6000 chars per page
- NEVER re-fetch a URL you have already read in this conversation
- After 5-6 tool calls total, STOP and write your complete synthesized answer
- If you have called 6 or more tools, you MUST write your final answer now — do not call any more tools"""


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

    if existing is None:
        client.post(
            "/api/v1/tools/create",
            json={
                "id": TOOL_ID,
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "content": TOOL_CONTENT,
                "meta": {"description": TOOL_DESCRIPTION},
            },
        ).raise_for_status()
        print(f"Created tool '{TOOL_NAME}'")
    else:
        client.post(
            f"/api/v1/tools/id/{TOOL_ID}/update",
            json={
                "id": TOOL_ID,
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "content": TOOL_CONTENT,
                "meta": {"description": TOOL_DESCRIPTION},
            },
        ).raise_for_status()
        print(f"Updated tool '{TOOL_NAME}' (env-based PRAETOR_API_KEY)")


def ensure_custom_model(client: httpx.Client) -> None:
    models = client.get("/api/v1/models/base").raise_for_status().json()
    existing = next((m for m in models if m.get("id") == CUSTOM_MODEL_ID), None)

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
                "memory": False, "builtin_tools": False,
            },
            "builtinTools": {
                "chats": False, "calendar": False, "tasks": False, "memory": False,
                "notes": False, "channels": False, "web_search": False,
                "automations": False, "image_generation": False,
                "code_interpreter": False, "time": False, "knowledge": True,
            },
            "toolIds": ["server:mcp:lm", "praetor_dispatch"],
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
        print(f"Custom model '{CUSTOM_MODEL_ID}' already exists — settings verified")


def deactivate_base_model(client: httpx.Client) -> None:
    models = client.get("/api/v1/models/base").raise_for_status().json()
    base = next(
        (m for m in models if m.get("id") == BASE_MODEL_ID and m.get("base_model_id") is None),
        None,
    )
    if base is None:
        return
    if not base.get("is_active", True):
        print(f"Base model '{BASE_MODEL_ID}' already inactive")
        return
    base["is_active"] = False
    client.post("/api/v1/models/model/update", json=base).raise_for_status()
    print(f"Deactivated base model '{BASE_MODEL_ID}'")


def main() -> None:
    if not OWUI_ADMIN_PASSWORD:
        print("Error: OWUI_ADMIN_PASSWORD not set", file=sys.stderr)
        sys.exit(1)

    with httpx.Client(base_url=OWUI_BASE_URL, timeout=15) as client:
        token = login(client)
        client.headers["Authorization"] = f"Bearer {token}"
        ensure_tool(client)
        ensure_custom_model(client)
        deactivate_base_model(client)
        print("Done.")


if __name__ == "__main__":
    main()
