"""PydanticAI PR reviewer agent: fetches diff, posts structured review."""
import os

import httpx
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel

from common.github_app import get_reviewer_installation_token

SYSTEM_PROMPT = """You are Cicero, an automated PR reviewer. When given a repo and PR number, you:
1. Call get_github_token to obtain an installation token
2. Call fetch_pr_diff(repo, pr_number, token) to get the unified diff
3. Analyze the diff for: correctness bugs, security issues (OWASP top 10), inefficiencies, missing error handling
4. Call post_review_comment(repo, pr_number, body, token) with a structured comment.

The comment body MUST start with this exact header line:
> 🏛️ **Cicero** — automated review

Then include the following sections:

## Summary
(1–2 sentences describing what the PR does)

## Issues
- [critical/major/minor] Description of each issue

## Suggestions
(optional improvements — omit section if none)

## Verdict
APPROVE / REQUEST_CHANGES / COMMENT

Keep feedback actionable and specific (reference file paths and line numbers where possible).
"""


def get_github_token() -> str:
    """Get a short-lived reviewer GitHub App installation token."""
    return get_reviewer_installation_token()


def fetch_pr_diff(repo: str, pr_number: int, token: str) -> str:
    """Fetch the unified diff for a pull request (capped at 32KB)."""
    resp = httpx.get(
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.diff",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text[:32000]


def post_review_comment(repo: str, pr_number: int, body: str, token: str) -> str:
    """Post a review comment on a GitHub PR. Returns the review URL."""
    resp = httpx.post(
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"body": body, "event": "COMMENT"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("html_url", "review posted")


def build_agent() -> Agent:
    model = OpenAIModel(
        model_name=os.environ.get("LLM_MODEL", "qwen3-35b"),
        base_url=os.environ["LITELLM_BASE_URL"],
        api_key=os.environ["LITELLM_API_KEY"],
    )
    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[get_github_token, fetch_pr_diff, post_review_comment],
    )
