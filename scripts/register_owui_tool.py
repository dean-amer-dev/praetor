#!/usr/bin/env python3
"""
Register the Praetor dispatch tool in OpenWebUI.

Run once after deployment:
  OWUI_BASE_URL=https://claw.amer.dev OWUI_API_KEY=<admin-key> python scripts/register_owui_tool.py

The PRAETOR_API_KEY valve must then be set manually in the OpenWebUI admin panel
(Admin → Tools → Praetor Dispatch → Edit → Valves).
"""
from __future__ import annotations

import json
import os
import sys

import httpx

TOOL_NAME = "Praetor Dispatch"
TOOL_DESCRIPTION = "Dispatch Praetor agent tasks (research, code, pipeline) and check their status."

TOOL_CONTENT = '''"""Praetor Agent Dispatch"""
import httpx
from pydantic import BaseModel


class Tools:
    class Valves(BaseModel):
        PRAETOR_BASE_URL: str = "https://praetor.amer.dev"
        PRAETOR_API_KEY: str = ""

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


def main() -> None:
    base = os.environ.get("OWUI_BASE_URL", "https://claw.amer.dev").rstrip("/")
    key = os.environ.get("OWUI_API_KEY", "")
    if not key:
        print("Error: OWUI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    with httpx.Client(base_url=base, headers=headers, timeout=15) as client:
        # Check if tool already exists
        resp = client.get("/api/v1/tools/")
        resp.raise_for_status()
        existing = [t for t in resp.json() if t.get("name") == TOOL_NAME]
        if existing:
            print(f"Tool '{TOOL_NAME}' already registered (id={existing[0]['id']}). Skipping.")
            return

        resp = client.post(
            "/api/v1/tools/create",
            json={
                "id": "praetor_dispatch",
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "content": TOOL_CONTENT,
                "meta": {"description": TOOL_DESCRIPTION},
            },
        )
        resp.raise_for_status()
        tool = resp.json()
        print(f"Registered tool '{TOOL_NAME}' id={tool.get('id')}")
        print("Next: set PRAETOR_API_KEY valve in Admin → Tools → Praetor Dispatch → Edit → Valves")


if __name__ == "__main__":
    main()
