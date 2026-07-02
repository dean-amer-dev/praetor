#!/usr/bin/env python3
"""
Bootstrap OpenWebUI for Praetor tool calling.

Idempotent — safe to run after any OWU reset or redeploy.

Run:
  OWUI_BASE_URL=https://bot.amer.dev OWUI_ADMIN_EMAIL=alex@amer.dev \
  OWUI_ADMIN_PASSWORD=<password> python scripts/register_owui_tool.py

What it does:
  1. Ensures the praetor_dispatch Python tool exists (dispatch_task + get_task_status +
     skills management: list_skills, create_skill, assign_skill, remove_skill_assignment;
     web_search/web_read_url come from the LiteLLM MCP Gateway tool, not here)
  2. Ensures the date_injector global Filter exists — prepends "Today is <date>" to every
     system prompt so the model can do date-accurate searches
  3. Ensures the qwen3-35b-think-custom model exists with:
       - function_calling=native
       - toolIds: ["praetor_dispatch", "server:mcp:lm"]
       - minimal behavioral system prompt (date line comes from the filter)
  4. Ensures the praetor-planner OWU custom model exists with:
       - function_calling=native, base_model=qwen3-35b-think
       - toolIds: ["server:mcp:lm"]
       - system prompt fetched dynamically from Langfuse ("planner-system", production)
  5. server:mcp:lm (LiteLLM MCP Gateway) is registered by OWU's MCP server config, not here.
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
TOOL_DESCRIPTION = "Dispatch Praetor agent tasks (code, pipeline) and manage agent skills."

TOOL_CONTENT = '''\
"""Praetor Agent Dispatch + Skills Management"""
import os
import httpx
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        PRAETOR_BASE_URL: str = "https://praetor.amer.dev"
        PRAETOR_API_KEY: str = Field(
            default_factory=lambda: os.environ.get("PRAETOR_API_KEY", "dRykVJyZp79Ute6JRKlZAgTuMs2jMXodKpszRyj-8aY")
        )

    def __init__(self):
        self.valves = self.Valves()

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.valves.PRAETOR_API_KEY}"}

    def _base(self) -> str:
        return self.valves.PRAETOR_BASE_URL

    # ------------------------------------------------------------------
    # Agent dispatch
    # ------------------------------------------------------------------

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
            f"{self._base()}/api/v1/dispatch",
            json={"title": title, "description": description, "type": task_type},
            headers=self._h(), timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return f"Task {data[\'task_id\']} dispatched ({data[\'event\']}). Check status in a few minutes."

    def get_task_status(self, task_id: int) -> str:
        """Check the status of a previously dispatched Praetor task."""
        resp = httpx.get(
            f"{self._base()}/api/v1/status/{task_id}",
            headers=self._h(), timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data["done"]:
            return f"Done. {data[\'mem0_summary\']}"
        return "Still running. Check back shortly."

    # ------------------------------------------------------------------
    # Skills management
    # ------------------------------------------------------------------

    def list_skills(self) -> str:
        """
        List all skills and their current agent assignments.
        Returns every skill (name, description) and which agents have it active.
        """
        skills_resp = httpx.get(f"{self._base()}/api/v1/skills", headers=self._h(), timeout=10)
        skills_resp.raise_for_status()
        skills = skills_resp.json()
        agents_resp = httpx.get(f"{self._base()}/api/v1/agents", headers=self._h(), timeout=10)
        agents_resp.raise_for_status()
        agents = agents_resp.json()

        agent_map: dict[str, list[str]] = {}
        for a in agents:
            for s in a.get("skills", []):
                agent_map.setdefault(s, []).append(a["agent_name"])

        if not skills:
            return "No skills defined yet. Use create_skill to add one."
        lines = ["**Skills:**"]
        for s in skills:
            assigned = agent_map.get(s["name"], [])
            assigned_str = ", ".join(assigned) if assigned else "unassigned"
            lines.append(f"- **{s[\'name\']}** ({assigned_str}): {s[\'description\']}")
        return "\\n".join(lines)

    def create_skill(self, name: str, description: str, prompt: str) -> str:
        """
        Create a new agent skill. The prompt is injected into the agent\'s system prompt
        at task-start — no pod restart needed, takes effect immediately.

        name: kebab-case identifier, e.g. "write-tests" or "add-logging"
        description: one-line summary of what this skill teaches the agent
        prompt: raw text appended to the agent\'s system prompt. Write it as a directive,
                e.g. "== SKILL: Write tests ==\\nAlways write pytest tests for every function..."
        """
        resp = httpx.post(
            f"{self._base()}/api/v1/skills",
            json={"name": name, "description": description, "prompt": prompt},
            headers=self._h(), timeout=10,
        )
        if resp.status_code == 409:
            return f\'Skill "{name}" already exists. Use update_skill to change its prompt.\'
        resp.raise_for_status()
        data = resp.json()
        return f\'Skill "{data["name"]}" created. Assign it to an agent with assign_skill.\'

    def assign_skill(self, agent_name: str, skill_name: str) -> str:
        """
        Assign a skill to an agent. Takes effect on the next task — no restart needed.
        agent_name: coder | research | reviewer | qa
        skill_name: name of an existing skill (use list_skills to see options)
        """
        resp = httpx.post(
            f"{self._base()}/api/v1/agents/{agent_name}/skills",
            json={"skill_name": skill_name},
            headers=self._h(), timeout=10,
        )
        if resp.status_code == 404:
            return f\'Skill "{skill_name}" not found. Create it first with create_skill.\'
        resp.raise_for_status()
        data = resp.json()
        skills_list = ", ".join(data["skills"]) if data["skills"] else "none"
        return f\'Assigned "{skill_name}" to {agent_name}. {agent_name} active skills: [{skills_list}]\'

    def remove_skill_assignment(self, agent_name: str, skill_name: str) -> str:
        """Remove a skill assignment from an agent."""
        resp = httpx.delete(
            f"{self._base()}/api/v1/agents/{agent_name}/skills/{skill_name}",
            headers=self._h(), timeout=10,
        )
        if resp.status_code == 404:
            return f\'Assignment {agent_name}/{skill_name} not found.\'
        resp.raise_for_status()
        return f\'Removed "{skill_name}" from {agent_name}.\'
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
PLANNER_MODEL_ID = "praetor-planner"
PLANNER_MODEL_NAME = "praetor-planner"

SYSTEM_PROMPT = """\
You are a helpful personal assistant with access to web search, GitHub, infrastructure, \
Praetor agent dispatch, agent factory tools, and agent skills management tools.

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
- task_type="code" — lighter code tasks via Praetor coder

## Agent skills management
Skills are prompt snippets injected into an agent's system prompt at task-start — \
no pod restart needed, takes effect immediately.
- list_skills() — see all skills and which agents have them
- create_skill(name, description, prompt) — define a new skill
- assign_skill(agent_name, skill_name) — activate skill for an agent (coder | research | reviewer | qa)
- remove_skill_assignment(agent_name, skill_name) — deactivate

Example flow: user says "teach the coder to always write tests" →
  1. create_skill("write-tests", "Ensures pytest tests are written", "== SKILL: Write tests ==\\nAlways write pytest tests...")
  2. assign_skill("coder", "write-tests")
  Done — next coder task will include the skill.

## General
After 5–6 tool calls on a research question, stop and write your answer. \
Never re-fetch a URL already read in this conversation."""


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


def _fetch_langfuse_prompt(client: httpx.Client) -> str:
    """Fetch planner system prompt from Langfuse production version.

    Reads credentials from env: LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY.
    Returns empty string on failure (model will have no system prompt).
    """
    try:
        from langfuse import Langfuse
        lf = Langfuse()  # reads LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY from env
        prompt = lf.get_prompt("planner-system", label="production")
        return prompt.prompt if hasattr(prompt, "prompt") else str(prompt)
    except Exception as exc:
        print(f"Warning: could not fetch planner system prompt from Langfuse: {exc}", file=sys.stderr)
        return ""


def ensure_planner_model(client: httpx.Client) -> None:
    """Ensure the praetor-planner OWU custom model exists."""
    resp = client.get(f"/api/v1/models/model?id={PLANNER_MODEL_ID}")
    existing = resp.json() if resp.status_code == 200 else None

    # Fetch system prompt from Langfuse (may be empty string on failure)
    system_prompt = _fetch_langfuse_prompt(client)

    model_payload = {
        "id": PLANNER_MODEL_ID,
        "name": PLANNER_MODEL_NAME,
        "base_model_id": BASE_MODEL_ID,
        "params": {"function_calling": "native"},
        "meta": {
            "profile_image_url": "",
            "description": "Praetor Planner — translates requests to TOML specs and dispatches",
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
            # server:mcp:lm provides lm_praetor_memory_search and praetor_execute_spec
            "toolIds": ["server:mcp:lm"],
            "system": system_prompt,
        },
        "is_active": True,
        "access_grants": [],
    }

    if existing is None:
        client.post("/api/v1/models/create", json=model_payload).raise_for_status()
        print(f"Created planner model '{PLANNER_MODEL_ID}'")
    else:
        client.post("/api/v1/models/model/update", json=model_payload).raise_for_status()
        print(f"Planner model '{PLANNER_MODEL_ID}' updated")

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
        ensure_planner_model(client)
        # NOTE: deactivate_base_model was removed — deactivating qwen3-35b-think breaks
        # custom model routing in OWU 0.9.6 (custom models route through their base_model_id,
        # which OWU requires to be active in the model list)
        print("Done.")


if __name__ == "__main__":
    main()
