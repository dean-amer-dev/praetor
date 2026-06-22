"""PydanticAI coder agent: clones repos, implements tasks, opens draft PRs."""
import asyncio
import os
import subprocess
from pathlib import Path

import httpx
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from common.memory_tools import add_memory, search_memory
from common.github_app import get_installation_token
from common.langfuse_tools import get_system_prompt, observe

SCRATCH_DIR = os.environ.get("SCRATCH_DIR", "/tmp/scratch")

_CODER_SYSTEM_PROMPT_FALLBACK = """You are a coder agent. Given a task title, description, and repo reference, you:
0. FIRST: call search_memory(query=<task title + description>, agent_id="coder-{owner}/{repo}")
   replacing {owner}/{repo} with the actual target repo (e.g. "coder-amerenda/ecdysis").
   Check for relevant past decisions, known patterns, or pitfalls before touching any code.
1. Retrieve a GitHub installation token via get_github_token(repo="{owner}/{repo}") — always pass
   the target repo so the correct installation is used (supports any org or user account):
   TOKEN = get_github_token(repo="owner/repo-name")
2. Clone the repo directly into SCRATCH_DIR (the dot clones into the current directory):
   git clone https://x-access-token:{TOKEN}@github.com/{repo}.git .
   SCRATCH_DIR is already clean — do NOT create a subdirectory.
3. Create a branch named praetor-coder/task-{task_id}
4. Implement the requested change using read_file, write_file, and run_shell
5. Commit the changes as: git -c user.name="scriptor[bot]" -c user.email="praetor-coder[bot]@users.noreply.github.com" commit -m "..."
6. Push the branch
7. Open a draft PR using the GitHub REST API (POST /repos/{owner}/{repo}/pulls with draft=true)
   - Include the Vikunja task ID in the PR description
   - Set base branch to main (or master if main doesn't exist)
8. Post the PR URL as a comment on the Vikunja task and mark it done

IMPORTANT — file paths:
- read_file and write_file paths are relative to SCRATCH_DIR, which IS the repo root after cloning with `.`
- Correct:   read_file("backend/main.py")
- Wrong:     read_file("ecdysis/backend/main.py")  ← never include the repo name as a prefix

Use run_shell for all git operations (cwd is SCRATCH_DIR = repo root).
Store key decisions in memory under agent_id='coder-{owner}/{repo}' (use the actual repo path).
"""


def _scratch(path: str) -> str:
    """Resolve path relative to SCRATCH_DIR, blocking traversal."""
    resolved = (Path(SCRATCH_DIR) / path).resolve()
    base = Path(SCRATCH_DIR).resolve()
    if not str(resolved).startswith(str(base)):
        raise ValueError(f"path traversal blocked: {path}")
    return str(resolved)


_MAX_TOOL_OUTPUT = 64 * 1024       # 64KB returned to message history
_MAX_SUBPROCESS_CAPTURE = 512 * 1024  # 512KB buffered before truncation (we truncate to 64KB anyway)


def _truncate(text: str, limit: int = _MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    kept = text[-limit:]
    dropped = len(text) - limit
    return f"[... {dropped} bytes truncated ...]\n{kept}"


@observe()
async def read_file(path: str) -> str:
    """Read a file relative to SCRATCH_DIR."""
    # Guard: if SCRATCH_DIR has no .git dir, the repo has not been cloned yet.
    if not (Path(SCRATCH_DIR) / ".git").exists():
        return (
            "ERROR: repository not cloned yet. "
            "You must call run_shell('git clone https://x-access-token:{TOKEN}@github.com/{repo}.git .') "
            "before reading files. SCRATCH_DIR is currently empty."
        )
    full = _scratch(path)
    loop = asyncio.get_event_loop()
    try:
        text = await loop.run_in_executor(None, lambda: Path(full).read_text(errors="replace"))
    except FileNotFoundError:
        return f"ERROR: file not found: {path}. Use run_shell('ls') to list available files."
    return _truncate(text)


@observe()
def write_file(path: str, content: str) -> str:
    """Write a file relative to SCRATCH_DIR, creating parent dirs as needed."""
    full = _scratch(path)
    Path(full).parent.mkdir(parents=True, exist_ok=True)
    Path(full).write_text(content)
    return f"wrote {path}"


@observe()
async def run_shell(cmd: str) -> str:
    """Run a shell command with cwd=SCRATCH_DIR. Returns stdout+stderr."""
    if ".." in cmd and ("/" in cmd or "\\" in cmd):
        # Block path-traversal patterns like ../../etc
        raise ValueError("path traversal blocked in shell command")
    Path(SCRATCH_DIR).mkdir(parents=True, exist_ok=True)

    def _run() -> str:
        # Stream output in chunks so a large-output subprocess can't OOM the process.
        # Subprocess writes to a pipe; we read up to _MAX_SUBPROCESS_CAPTURE bytes
        # and discard the rest (still draining the pipe to avoid blocking the child).
        with subprocess.Popen(
            cmd, shell=True, cwd=SCRATCH_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        ) as proc:
            chunks: list[str] = []
            total = 0
            overflow = False
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                if total < _MAX_SUBPROCESS_CAPTURE:
                    keep = min(len(chunk), _MAX_SUBPROCESS_CAPTURE - total)
                    chunks.append(chunk[:keep])
                    total += keep
                    if total >= _MAX_SUBPROCESS_CAPTURE:
                        overflow = True
                # Always drain to avoid blocking the subprocess
            try:
                proc.wait(timeout=300)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                chunks.append("\n[subprocess timed out after 300s]")
            output = "".join(chunks)
            if overflow:
                output += f"\n[subprocess output exceeded {_MAX_SUBPROCESS_CAPTURE // 1024 // 1024}MB and was truncated]"
            return _truncate(output) if output else f"(exit {proc.returncode})"

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


@observe()
def get_github_token(repo: str | None = None) -> str:
    """Retrieve a short-lived GitHub App installation token for API calls.

    Pass repo='owner/repo' to get a token scoped to any org or user account
    where the praetor-coder app is installed. Omit to use the default installation.
    """
    return get_installation_token(repo=repo)


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
        if comment_resp.status_code == 404:
            return "note: Vikunja task not found — coder complete, task tracking skipped"
        comment_resp.raise_for_status()
        if done:
            done_resp = await client.post(
                f"{base}/api/v1/tasks/{task_id}",
                json={"done": True},
                headers=headers,
            )
            if done_resp.status_code not in (200, 404):
                done_resp.raise_for_status()
    return "task updated"


def build_agent() -> Agent:
    model = OpenAIChatModel(
        model_name=os.environ.get("LLM_MODEL", "coder"),
        provider=OpenAIProvider(
            base_url=os.environ["LITELLM_BASE_URL"],
            api_key=os.environ["LITELLM_API_KEY"],
        ),
    )
    return Agent(
        model=model,
        system_prompt=get_system_prompt("coder-system", fallback=_CODER_SYSTEM_PROMPT_FALLBACK),
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
