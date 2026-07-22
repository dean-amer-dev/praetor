"""Benchmark agent: runs a target agent against a dataset item and scores the output."""
from __future__ import annotations

import json
import os

import httpx
from langfuse import Langfuse

_RESEARCH_SYSTEM_PROMPT = (
    "You are a research agent. Given a topic, research it thoroughly and provide a "
    "concise, well-structured markdown report. Include key findings, relevant comparisons, "
    "and practical recommendations where applicable. Cite specific details and be precise."
)


def _langfuse() -> Langfuse:
    return Langfuse(
        host=os.environ["LANGFUSE_HOST"],
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
    )


def score_output(agent_output: str, expected_criteria: dict) -> tuple[float, str]:
    """Ask the LLM judge to score the agent output. Returns (score, reason)."""
    criteria_text = json.dumps(expected_criteria, indent=2)
    judge_prompt = (
        f"Score the following agent output from 0.0 to 1.0 based on how well it meets the expected criteria.\n\n"
        f"Expected criteria:\n{criteria_text}\n\n"
        f"Agent output:\n{agent_output}\n\n"
        f'Return only a JSON object: {{"score": <float 0.0-1.0>, "reason": "<one sentence>"}}'
    )
    resp = httpx.post(
        f"{os.environ['LITELLM_BASE_URL']}/chat/completions",
        json={
            "model": os.environ.get("LLM_MODEL", "coder"),
            "messages": [{"role": "user", "content": judge_prompt}],
            "max_tokens": 128,
        },
        headers={"Authorization": f"Bearer {os.environ['LITELLM_API_KEY']}"},
        timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    # Strip any markdown code fences
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    try:
        parsed = json.loads(content)
        return float(parsed["score"]), str(parsed.get("reason", ""))
    except Exception:
        return 0.0, f"judge returned unparseable output: {content[:100]}"


async def run_research_benchmark(item_input: dict, agent_id: str) -> str:
    """Evaluate research quality via direct LLM call.

    Uses a direct completion rather than the pydantic-ai agent to avoid the
    memory footprint of loading the MCP stack inside the benchmark worker.
    """
    task_title = item_input.get("task_title", item_input.get("input", ""))
    task_description = item_input.get("task_description", "")
    user_prompt = f"Research topic: {task_title}"
    if task_description:
        user_prompt += f"\n\nDetails: {task_description}"
    user_prompt += "\n\nProvide a thorough markdown research report with key findings."
    resp = httpx.post(
        f"{os.environ['LITELLM_BASE_URL']}/chat/completions",
        json={
            "model": os.environ.get("LLM_MODEL", "coder"),
            "messages": [
                {"role": "system", "content": _RESEARCH_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 2048,
        },
        headers={"Authorization": f"Bearer {os.environ['LITELLM_API_KEY']}"},
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


async def run_reviewer_benchmark(item_input: dict) -> str:
    """Score reviewer quality against a synthetic diff without GitHub tool calls.

    Uses a direct LLM completion with the reviewer system prompt so that
    no GitHub App credentials are needed in the benchmark worker.
    """
    from agents.pr_reviewer.agent import SYSTEM_PROMPT

    diff = item_input.get("diff", "")
    repo = item_input.get("repo", "amerenda/praetor")
    pr_number = item_input.get("pr_number", "0")
    user_prompt = (
        f"Review PR #{pr_number} on {repo}. "
        f"Here is the diff:\n```diff\n{diff}\n```\n\n"
        "Return your review as structured text. Do not call any tools."
    )
    resp = httpx.post(
        f"{os.environ['LITELLM_BASE_URL']}/chat/completions",
        json={
            "model": os.environ.get("LLM_MODEL", "coder"),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 1024,
        },
        headers={"Authorization": f"Bearer {os.environ['LITELLM_API_KEY']}"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]
