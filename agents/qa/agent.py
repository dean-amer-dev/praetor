"""PydanticAI QA agent: tests staging deployments with HTTP checks and playwright."""
import os

import httpx
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

SYSTEM_PROMPT = """You are a QA agent. Given a staging deployment URL, you:
1. Call http_check(url) to verify the URL returns 2xx
2. Call playwright_check(url) to load the page with a headless browser and check for JS errors
3. Summarize pass/fail results and call post_qa_result with the outcome

Report clearly: which checks passed, which failed, and any error messages observed.
"""


def http_check(url: str) -> dict:
    """HTTP GET the URL. Returns status code and whether it's 2xx."""
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        return {"url": url, "status": resp.status_code, "ok": resp.is_success}
    except Exception as exc:
        return {"url": url, "error": str(exc), "ok": False}


def playwright_check(url: str) -> dict:
    """Load the page with playwright headless Chromium. Returns title and any JS errors."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(url, timeout=30000)
            title = page.title()
            browser.close()
            return {"url": url, "title": title, "console_errors": errors, "ok": len(errors) == 0}
    except Exception as exc:
        return {"url": url, "error": str(exc), "ok": False}


def post_qa_result(repo: str, pr_number: int | None, summary: str, passed: bool) -> str:
    """Record QA results. Returns a formatted result string."""
    status = "PASSED" if passed else "FAILED"
    ref = f" PR#{pr_number}" if pr_number else ""
    result = f"QA {status} for {repo}{ref}: {summary}"
    print(result)
    return result


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
        tools=[http_check, playwright_check, post_qa_result],
    )
