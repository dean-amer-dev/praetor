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
6. BEFORE pushing — syntax-check every Python file you modified:
   List changed files with run_shell("git diff --name-only HEAD"), then for each .py file
   run run_shell("python -m py_compile <that_file>"). Fix any SyntaxError before continuing.
7. Push the branch
8. Open a draft PR using the GitHub REST API (POST /repos/{owner}/{repo}/pulls with draft=true)
   - Include the Vikunja task ID in the PR description
   - Set base branch to main (or master if main doesn't exist)
9. Post the PR URL as a comment on the Vikunja task and mark it done

IMPORTANT — file paths:
- read_file and write_file paths are relative to SCRATCH_DIR, which IS the repo root after cloning with `.`
- Correct:   read_file("backend/main.py")
- Wrong:     read_file("ecdysis/backend/main.py")  ← never include the repo name as a prefix

IMPORTANT — k8s manifest versions:
Before generating any Kubernetes manifest with an apiVersion from an operator CRD
(e.g. external-secrets.io/*, keda.sh/*, cert-manager.io/*), run:
  run_shell("kubectl api-resources --api-group=<group> 2>&1")
Use whatever version is reported. Do NOT use training data to guess — installed versions differ from defaults.

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


_MAX_TOOL_OUTPUT = 64 * 1024       # 64KB hard cap returned to message history
_MAX_SUBPROCESS_CAPTURE = 512 * 1024  # 512KB buffered before truncation
_SHELL_TAIL_LIMIT = 3000           # keep tail of shell output (errors at the end)
_FILE_HEAD_LIMIT = 8000            # keep head of file reads (structure at the top)


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
    if len(text) > _FILE_HEAD_LIMIT:
        return text[:_FILE_HEAD_LIMIT] + f"\n[... {len(text) - _FILE_HEAD_LIMIT} chars truncated ...]"
    return text


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
            if not output:
                return f"(exit {proc.returncode})"
            if len(output) > _SHELL_TAIL_LIMIT:
                dropped = len(output) - _SHELL_TAIL_LIMIT
                return f"[... {dropped} chars truncated ...]\n" + output[-_SHELL_TAIL_LIMIT:]
            return output

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
async def github_api(
    method: str,
    path: str,
    body: dict | None = None,
    repo: str | None = None,
) -> dict:
    """Call the GitHub REST API authenticated as praetor-coder.

    method: HTTP verb — "GET", "POST", "PATCH", "PUT", "DELETE"
    path:   API path starting with /, e.g. "/repos/amerenda/k3s-dean-gitops/pulls"
    body:   JSON body for POST/PATCH/PUT (omit for GET/DELETE)
    repo:   "owner/repo" used to scope the installation token — required for writes

    Returns the parsed JSON response, or {"status": "ok"} for 204 responses,
    or {"error": "HTTP N", "body": "..."} on failure.
    """
    token = get_installation_token(repo=repo)
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async def _call() -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                method.upper(),
                f"https://api.github.com{path}",
                headers=headers,
                json=body,
            )
            if not resp.is_success:
                return {"error": f"HTTP {resp.status_code}", "body": resp.text[:2000]}
            if resp.status_code == 204:
                return {"status": "ok"}
            return resp.json()

    return await _call()


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


@observe()
async def save_progress(task_id: int, done: list[str], remaining: list[str], notes: str = "") -> str:
    """
    Checkpoint task progress to Mem0. Call every 8-10 actions.

    task_id: the current task ID (from the task prompt header)
    done: features or files completed so far
    remaining: features or files still to implement
    notes: current state, key decisions, anything needed on resume

    If the run is retried or resumed, prior checkpoints appear in search_memory results
    and should be used to skip already-completed work.
    """
    content = (
        f"CHECKPOINT task-{task_id}\n"
        f"done: {', '.join(done)}\n"
        f"remaining: {', '.join(remaining)}\n"
        f"notes: {notes}"
    )
    await add_memory(content, f"task-{task_id}")
    return f"checkpoint saved — {len(remaining)} items remaining"


def build_agent(system_prompt: str | None = None) -> Agent:
    model = OpenAIChatModel(
        model_name=os.environ.get("LLM_MODEL", "coder"),
        provider=OpenAIProvider(
            base_url=os.environ["LITELLM_BASE_URL"],
            api_key=os.environ["LITELLM_API_KEY"],
        ),
    )
    if system_prompt is None:
        system_prompt = get_system_prompt("coder-system", fallback=_CODER_SYSTEM_PROMPT_FALLBACK)
    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[
            read_file,
            write_file,
            run_shell,
            get_github_token,
            github_api,
            add_memory,
            search_memory,
            save_progress,
            update_vikunja_task,
        ],
    )
