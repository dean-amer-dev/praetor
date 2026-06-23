"""Hatchet worker: dispatches tasks to OpenHands via its REST API."""
import asyncio
import os
import time
from datetime import timedelta

import httpx
from hatchet_sdk import Context, Hatchet
from hatchet_sdk.types.concurrency import ConcurrencyExpression, ConcurrencyLimitStrategy
from pydantic import BaseModel

from common.memory_tools import add_memory, search_memory
from common.metrics import start_metrics_server, task_invocations, task_active, task_duration

_AGENT_NAME = "openhands"
_OPENHANDS_BASE = os.environ.get("OPENHANDS_BASE_URL", "http://openhands.openhands.svc.cluster.local")
_OPENHANDS_UI = os.environ.get("OPENHANDS_UI_URL", "https://hands.amer.dev")
_POLL_INTERVAL = 30
_MAX_POLL_MINUTES = 15


class OpenHandsInput(BaseModel):
    task_id: int
    task_title: str
    task_description: str = ""


async def _create_conversation(task_text: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{_OPENHANDS_BASE}/api/conversations", json={})
        resp.raise_for_status()
        conversation_id = resp.json()["conversation_id"]

    # Wait for AWAITING_USER_INPUT before sending the message. OpenHands returns
    # "RUNNING" while the session is still loading (action server ~45s init), so
    # accepting RUNNING causes messages to be dropped. Must wait for the specific
    # AWAITING_USER_INPUT state which only appears after the session is truly ready.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        async with httpx.AsyncClient(timeout=10) as client:
            check = await client.get(f"{_OPENHANDS_BASE}/api/conversations/{conversation_id}")
            check.raise_for_status()
        state = check.json().get("status", "")
        if state == "AWAITING_USER_INPUT":
            break
        await asyncio.sleep(5)

    async with httpx.AsyncClient(timeout=30) as client:
        msg = await client.post(
            f"{_OPENHANDS_BASE}/api/conversations/{conversation_id}/message",
            json={"message": task_text},
        )
        msg.raise_for_status()
    return conversation_id


async def _poll_until_done(conversation_id: str) -> dict:
    deadline = time.monotonic() + _MAX_POLL_MINUTES * 60
    while time.monotonic() < deadline:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_OPENHANDS_BASE}/api/conversations/{conversation_id}")
            resp.raise_for_status()
            data = resp.json()
        status = data.get("status", "RUNNING")
        if status in ("FINISHED", "ERROR", "STOPPED"):
            return data
        await asyncio.sleep(_POLL_INTERVAL)
    return {"status": "TIMEOUT", "conversation_id": conversation_id}


async def _run_openhands(input: OpenHandsInput, context: Context) -> dict:
    task_text = f"Task #{input.task_id}: {input.task_title}"
    if input.task_description:
        task_text += f"\n\n{input.task_description}"

    prior = await search_memory(f"{input.task_title} {input.task_description}", _AGENT_NAME)
    if prior:
        task_text += "\n\nPrior context from memory:\n" + "\n".join(prior)

    task_active.labels(agent=_AGENT_NAME).inc()
    t0 = time.monotonic()
    try:
        conversation_id = await _create_conversation(task_text)
        result = await _poll_until_done(conversation_id)
        status = result.get("status", "UNKNOWN")
        url = f"{_OPENHANDS_UI}/conversations/{conversation_id}"

        summary = (
            f"Task #{input.task_id} ({input.task_title}): "
            f"status={status}, conversation={conversation_id}, url={url}"
        )
        await add_memory(summary, _AGENT_NAME)
        await add_memory(summary, f"task-{input.task_id}")

        task_invocations.labels(agent=_AGENT_NAME, status="success").inc()
        return {
            "task_id": input.task_id,
            "conversation_id": conversation_id,
            "status": status,
            "url": url,
        }
    except Exception:
        task_invocations.labels(agent=_AGENT_NAME, status="error").inc()
        raise
    finally:
        task_active.labels(agent=_AGENT_NAME).dec()
        task_duration.labels(agent=_AGENT_NAME).observe(time.monotonic() - t0)


def main() -> None:
    start_metrics_server(_AGENT_NAME)
    hatchet = Hatchet()

    run_openhands = hatchet.task(
        name="openhands",
        on_events=["agent:openhands"],
        input_validator=OpenHandsInput,
        execution_timeout=timedelta(minutes=20),
        retries=0,
        concurrency=ConcurrencyExpression(
            expression='"openhands"',
            max_runs=1,
            limit_strategy=ConcurrencyLimitStrategy.CANCEL_NEWEST,
        ),
    )(_run_openhands)

    worker = hatchet.worker("openhands-worker", workflows=[run_openhands], slots=1)
    worker.start()


if __name__ == "__main__":
    main()
