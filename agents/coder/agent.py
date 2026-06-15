"""PydanticAI coder agent: clones repos, implements tasks, opens draft PRs."""
import os
import subprocess
from pathlib import Path

import httpx
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerHTTP
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from common.memory_tools import add_memory, search_memory
from common.github_app import get_installation_token
from common.langfuse_tools import get_system_prompt, observe

SCRATCH_DIR = os.environ.get("SCRATCH_DIR", "/tmp/scratch")

_CODER_SYSTEM_PROMPT_FALLBACK = """You are a coder agent. Given a task title, description, and repo reference, you:
1. Retrieve a GitHub installation token via get_github_token
2. Clone the repo to SCRATCH_DIR using: git clone https://x-access-token:{TOKEN}@github.com/{repo}.git
3. Create a branch named amerenda-coder/task-{task_id}
4. Implement the requested change using read_file, write_file, and run_shell
5. Commit the changes as: git -c user.name="scriptor[bot]" -c user.email="amerenda-coder[bot]@users.noreply.github.com" commit -m "..."
6. Push the branch
7. Open a draft PR using the GitHub REST API (POST /repos/{owner}/{repo}/pulls with draft=true)
   - Include the Vikunja task ID in the PR description
   - Set base branch to main (or master if main doesn't exist)
8. Post the PR URL as a comment on the Vikunja task and mark it done

Use run_shell for all git operations. Paths passed to read_file and write_file are relative to SCRATCH_DIR.
Store key decisions in memory under agent_id='task-{task_id}'.
"""


def _scratch(path: str) -> str:
    """Resolve path relative to SCRATCH_DIR, blocking traversal."""
    resolved = (Path(SCRATCH_DIR) / path).resolve()
    base = Path(SCRATCH_DIR).resolve()
    if not str(resolved).startswith(str(base)):
        raise ValueError(f"path traversal blocked: {path}")
    return str(resolved)


@observe()
def read_file(path: str) -> str:
    """Read a file relative to SCRATCH_DIR."""
    full = _scratch(path)
    return Path(full).read_text(errors="replace")


@observe()
def write_file(path: str, content: str) -> str:
    """Write a file relative to SCRATCH_DIR, creating parent dirs as needed."""
    full = _scratch(path)
    Path(full).parent.mkdir(parents=True, exist_ok=True)
    Path(full).write_text(content)
    return f"wrote {path}"


@observe()
def run_shell(cmd: str) -> str:
    """Run a shell command with cwd=SCRATCH_DIR. Returns stdout+stderr."""
    if ".." in cmd and ("/" in cmd or "\\" in cmd):
        # Block path-traversal patterns like ../../etc
        raise ValueError("path traversal blocked in shell command")
    Path(SCRATCH_DIR).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        cmd,
        shell=True,
        cwd=SCRATCH_DIR,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = result.stdout + result.stderr
    return output if output else f"(exit {result.returncode})"


@observe()
def get_github_token() -> str:
    """Retrieve a short-lived GitHub App installation token for API calls."""
    return get_installation_token()


@observe()
async def update_vikunja_task(task_id: int, comment: str, done: bool = True) -> str:
    """Update a Vikunja task: post a comment and optionally mark it done."""
    base = os.environ.get("VIKUNJA_BASE_URL", "https://todo.amer.dev")
    token = os.environ["VIKUNJA_TOKEN"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10) as client:
        comment_resp = await client.put(
            f"{base}/api/v1/tasks/{task_id}/comments",
            json={"comment": comment},
            headers=headers,
        )
        if comment_resp.status_code == 401:
            return "error: Vikunja token expired — coder complete but task not updated"
        comment_resp.raise_for_status()
        if done:
            done_resp = await client.post(
                f"{base}/api/v1/tasks/{task_id}",
                json={"done": True},
                headers=headers,
            )
            done_resp.raise_for_status()
    return "task updated"


def _build_mcp_server() -> MCPServerHTTP:
    mcp_url = os.environ.get(
        "LITELLM_MCP_URL",
        os.environ["LITELLM_BASE_URL"].replace("/v1", "/mcp"),
    )
    return MCPServerHTTP(
        url=mcp_url,
        headers={"Authorization": f"Bearer {os.environ['LITELLM_API_KEY']}"},
        read_timeout=60,
    )


def build_agent() -> Agent:
    model = OpenAIModel(
        model_name=os.environ.get("LLM_MODEL", "coder"),
        provider=OpenAIProvider(
            base_url=os.environ["LITELLM_BASE_URL"],
            api_key=os.environ["LITELLM_API_KEY"],
        ),
    )
    return Agent(
        model=model,
        system_prompt=get_system_prompt("coder-system", fallback=_CODER_SYSTEM_PROMPT_FALLBACK),
        mcp_servers=[_build_mcp_server()],
        tools=[
            read_file,
            write_file,
            run_shell,
            get_github_token,
            add_memory,
            search_memory,
            update_vikunja_task,
        ],
    )
