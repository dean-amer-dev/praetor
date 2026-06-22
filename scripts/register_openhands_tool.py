#!/usr/bin/env python3
"""
Register the OpenHands dispatch tool in OpenWebUI.

Run once after deployment:
  OWUI_BASE_URL=https://claw.amer.dev OWUI_API_KEY=<admin-key> python scripts/register_openhands_tool.py

The PRAETOR_API_KEY valve must then be set manually in the OpenWebUI admin panel
(Admin → Tools → OpenHands Dispatch → Edit → Valves).
"""
from __future__ import annotations

import os
import sys

import httpx

TOOL_NAME = "OpenHands Dispatch"
TOOL_DESCRIPTION = "Dispatch autonomous coding tasks to OpenHands and check their status."

TOOL_CONTENT = '''"""OpenHands Agent Dispatch"""
import httpx
from pydantic import BaseModel


class Tools:
    class Valves(BaseModel):
        PRAETOR_BASE_URL: str = "https://praetor.amer.dev"
        PRAETOR_API_KEY: str = ""

    def __init__(self):
        self.valves = self.Valves()

    def dispatch_openhands_task(self, title: str, description: str) -> str:
        """
        Dispatch an autonomous coding task to OpenHands.
        OpenHands will autonomously research, implement, and open a PR.
        Include \'repo: owner/name\' in the description to scope work to a specific repo.
        Returns a conversation URL where you can watch progress at hands.amer.dev.
        """
        resp = httpx.post(
            f"{self.valves.PRAETOR_BASE_URL}/api/v1/dispatch",
            json={"title": title, "description": description, "type": "openhands"},
            headers={"Authorization": f"Bearer {self.valves.PRAETOR_API_KEY}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return (
            f"OpenHands task {data[\'task_id\']} dispatched. "
            f"Watch progress at https://hands.amer.dev — "
            f"conversation will appear within a few seconds."
        )

    def get_openhands_status(self, task_id: int) -> str:
        """Check whether a previously dispatched OpenHands task has completed."""
        resp = httpx.get(
            f"{self.valves.PRAETOR_BASE_URL}/api/v1/status/{task_id}",
            headers={"Authorization": f"Bearer {self.valves.PRAETOR_API_KEY}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data["done"]:
            return f"Done. {data[\'mem0_summary\']}"
        return "Still running. Check hands.amer.dev or try again shortly."
'''


def main() -> None:
    base = os.environ.get("OWUI_BASE_URL", "https://claw.amer.dev").rstrip("/")
    key = os.environ.get("OWUI_API_KEY", "")
    if not key:
        print("Error: OWUI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    with httpx.Client(base_url=base, headers=headers, timeout=15) as client:
        resp = client.get("/api/v1/tools/")
        resp.raise_for_status()
        existing = [t for t in resp.json() if t.get("name") == TOOL_NAME]
        if existing:
            print(f"Tool '{TOOL_NAME}' already registered (id={existing[0]['id']}). Skipping.")
            return

        resp = client.post(
            "/api/v1/tools/create",
            json={
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "content": TOOL_CONTENT,
                "meta": {"description": TOOL_DESCRIPTION},
            },
        )
        resp.raise_for_status()
        tool = resp.json()
        print(f"Registered tool '{TOOL_NAME}' id={tool.get('id')}")
        print("Next: set PRAETOR_API_KEY valve in Admin → Tools → OpenHands Dispatch → Edit → Valves")


if __name__ == "__main__":
    main()
