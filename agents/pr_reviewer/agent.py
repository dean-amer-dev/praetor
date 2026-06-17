"""PydanticAI PR reviewer agent: fetches diff, posts structured review."""
import os

import httpx
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from common.github_app import get_reviewer_installation_token

SYSTEM_PROMPT = """You are Cicero, an automated PR reviewer. When given a repo and PR number, you:
1. Call get_github_token to obtain an installation token
2. Call fetch_pr_diff(repo, pr_number, token) to get the unified diff — the response includes HEAD_SHA and HEAD_BRANCH at the top
3. For each file changed in the diff, call fetch_file_content(repo, path, HEAD_SHA, token) to read the full file
4. Analyze the diff for: correctness bugs, security issues (OWASP top 10), inefficiencies, missing error handling
   IMPORTANT: Before flagging a pattern, check whether it already exists in the unchanged parts of the same file.
   If the pattern is already present elsewhere in the file, do NOT flag it — the PR did not introduce it.
   Only flag issues that are new to this PR or that this PR makes worse.
5. Call post_review_comment(repo, pr_number, body, event, token) with a structured comment.

The comment body MUST start with this exact header line:
> 🏛️ **Cicero** — automated review

Then include the following sections:

## Summary
(1–2 sentences describing what the PR does)

## Issues
- [critical/major/minor] Description of each issue
- (write "None" if no issues found)

## Suggestions
(optional improvements — omit section if none)

## Verdict
Pick exactly one based on your analysis:
- APPROVE — no issues, ready to merge
- REQUEST_CHANGES — has critical or major issues that must be fixed
- COMMENT — minor/informational feedback only

The `event` parameter to post_review_comment must match your verdict exactly: "APPROVE", "REQUEST_CHANGES", or "COMMENT".

Keep feedback actionable and specific (reference file paths and line numbers where possible).
"""


def get_github_token() -> str:
    """Get a short-lived reviewer GitHub App installation token."""
    return get_reviewer_installation_token()


def fetch_pr_diff(repo: str, pr_number: str, token: str) -> str:
    """Fetch the unified diff for a PR. Returns HEAD_SHA and HEAD_BRANCH on the first two lines, then the diff (capped at 32KB)."""
    headers = {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
    meta_resp = httpx.get(
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
        headers={**headers, "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    meta_resp.raise_for_status()
    meta = meta_resp.json()
    head_sha = meta["head"]["sha"]
    head_branch = meta["head"]["ref"]
    diff_resp = httpx.get(
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
        headers={**headers, "Accept": "application/vnd.github.v3.diff"},
        timeout=30,
        follow_redirects=True,
    )
    diff_resp.raise_for_status()
    return f"HEAD_SHA: {head_sha}\nHEAD_BRANCH: {head_branch}\n\n{diff_resp.text[:32000]}"


def fetch_file_content(repo: str, path: str, ref: str, token: str) -> str:
    """Fetch the full content of a file at a specific ref (commit SHA or branch name), capped at 16KB."""
    resp = httpx.get(
        f"https://api.github.com/repos/{repo}/contents/{path}",
        params={"ref": ref},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.raw",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=15,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text[:16000]


def post_review_comment(repo: str, pr_number: str, body: str, event: str, token: str) -> str:
    """Post a review on a GitHub PR. event must be APPROVE, REQUEST_CHANGES, or COMMENT."""
    valid_events = {"APPROVE", "REQUEST_CHANGES", "COMMENT"}
    if event not in valid_events:
        event = "COMMENT"
    resp = httpx.post(
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"body": body, "event": event},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("html_url", "review posted")


def build_agent() -> Agent:
    model = OpenAIModel(
        model_name=os.environ.get("LLM_MODEL", "qwen3-35b"),
        provider=OpenAIProvider(
            base_url=os.environ["LITELLM_BASE_URL"],
            api_key=os.environ["LITELLM_API_KEY"],
        ),
    )
    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[get_github_token, fetch_pr_diff, fetch_file_content, post_review_comment],
        model_settings={"temperature": 0},
    )
