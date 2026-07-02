"""Shared dispatch function — called by all trigger paths (Vikunja webhook, HTTP API, MCP)."""
from __future__ import annotations

from typing import Literal

from hatchet_sdk import Hatchet

AgentType = Literal["research", "code", "pipeline", "scaffold", "openhands", "feature_pipeline", "model_evaluate"]

EVENT_MAP: dict[str, str] = {
    "research":         "agent:research",
    "code":             "agent:code",
    "pipeline":         "pipeline:research_code",
    "scaffold":         "agent:scaffold",
    "openhands":        "agent:openhands",
    "feature_pipeline": "pipeline:feature_decompose",
    "model_evaluate":   "pipeline:model_evaluate",
}

_hatchet: Hatchet | None = None


def _get_hatchet() -> Hatchet:
    global _hatchet
    if _hatchet is None:
        _hatchet = Hatchet()
    return _hatchet


def dispatch_agent(
    task_id: int,
    task_title: str,
    task_description: str,
    agent_type: AgentType,
    additional_metadata: dict | None = None,
) -> list[str]:
    """Push the appropriate Hatchet event. Returns list of event names pushed."""
    event = EVENT_MAP[agent_type]
    payload = {
        "task_id": task_id,
        "task_title": task_title,
        "task_description": task_description,
    }
    _get_hatchet().event.push(event, payload, additional_metadata=additional_metadata or {})
    return [event]
