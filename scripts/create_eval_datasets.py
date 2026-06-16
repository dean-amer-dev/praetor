"""One-time script: create Langfuse eval datasets for Phase 13 benchmarking.

Run once to initialize the datasets. Safe to re-run — skips datasets that already exist.

Usage:
    python scripts/create_eval_datasets.py
"""
from __future__ import annotations

import os
import sys

from langfuse import Langfuse


def _client() -> Langfuse:
    for var in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        if not os.environ.get(var):
            print(f"ERROR: {var} not set", file=sys.stderr)
            sys.exit(1)
    return Langfuse(
        host=os.environ["LANGFUSE_HOST"],
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
    )


RESEARCH_ITEMS = [
    {
        "input": {
            "task_title": "Tailscale exit node ACL interaction",
            "task_description": "How do exit nodes interact with ACL policies in Tailscale?",
        },
        "expected_output": {
            "contains_keywords": ["exit node", "ACL", "subnet"],
            "min_length": 200,
            "is_markdown": True,
        },
    },
    {
        "input": {
            "task_title": "Hatchet durable execution model",
            "task_description": "Explain the Hatchet workflow engine's durable execution guarantees and retry semantics.",
        },
        "expected_output": {
            "contains_keywords": ["durable", "retry", "workflow", "step"],
            "min_length": 200,
            "is_markdown": True,
        },
    },
    {
        "input": {
            "task_title": "K3s vs K8s resource overhead comparison",
            "task_description": "Compare resource overhead between K3s and full Kubernetes for edge deployments.",
        },
        "expected_output": {
            "contains_keywords": ["K3s", "memory", "resource"],
            "min_length": 150,
            "is_markdown": True,
        },
    },
    {
        "input": {
            "task_title": "PydanticAI agent tool calling patterns",
            "task_description": "How does PydanticAI handle tool calling and structured output in agents?",
        },
        "expected_output": {
            "contains_keywords": ["tool", "agent", "pydantic"],
            "min_length": 150,
            "is_markdown": True,
        },
    },
    {
        "input": {
            "task_title": "Langfuse dataset eval workflow best practices",
            "task_description": "Best practices for creating and running eval datasets in Langfuse for LLM evaluation.",
        },
        "expected_output": {
            "contains_keywords": ["dataset", "score", "trace", "eval"],
            "min_length": 150,
            "is_markdown": True,
        },
    },
]

REVIEWER_ITEMS = [
    {
        "input": {
            "repo": "amerenda/praetor",
            "pr_number": "999",
            "diff": """\
--- a/app.py
+++ b/app.py
@@ -10,7 +10,7 @@ def get_user(user_id: str):
-    query = f"SELECT * FROM users WHERE id = '{user_id}'"
+    query = f"SELECT * FROM users WHERE id = '{user_id}'"
     result = db.execute(query)
     return result.fetchone()
""",
        },
        "expected_output": {
            "flags_security_issue": True,
            "mentions_keywords": ["injection", "parameterized", "unsafe", "SQL"],
            "verdict": "REQUEST_CHANGES",
        },
    },
    {
        "input": {
            "repo": "amerenda/praetor",
            "pr_number": "998",
            "diff": """\
--- a/agents/research/worker.py
+++ b/agents/research/worker.py
@@ -1,5 +1,6 @@
 from hatchet_sdk import Context, Hatchet
 from pydantic import BaseModel
+import logging

+log = logging.getLogger(__name__)

 class ResearchInput(BaseModel):
     task_id: int
""",
        },
        "expected_output": {
            "flags_security_issue": False,
            "verdict_is_positive": True,
            "mentions_keywords": ["logging", "clean", "good"],
        },
    },
    {
        "input": {
            "repo": "amerenda/praetor",
            "pr_number": "997",
            "diff": """\
--- a/webhooks/github_handler.py
+++ b/webhooks/github_handler.py
@@ -5,6 +5,8 @@ import os
 def handle_push(payload: dict) -> None:
     ref = payload["ref"]
-    os.system(f"git pull origin {ref}")
+    branch = ref.split("/")[-1]
+    subprocess.run(["git", "pull", "origin", branch], check=True)
""",
        },
        "expected_output": {
            "flags_security_issue": False,
            "mentions_improvement": True,
            "mentions_keywords": ["subprocess", "shell", "injection"],
        },
    },
    {
        "input": {
            "repo": "amerenda/praetor",
            "pr_number": "996",
            "diff": """\
--- a/common/dispatch.py
+++ b/common/dispatch.py
@@ -20,6 +20,7 @@ def dispatch_agent(
     task_description: str,
     agent_type: AgentType,
     additional_metadata: dict | None = None,
+    dry_run: bool = False,
 ) -> list[str]:
     event = EVENT_MAP[agent_type]
     payload = {
@@ -27,5 +28,7 @@ def dispatch_agent(
         "task_title": task_title,
         "task_description": task_description,
     }
-    _get_hatchet().event.push(event, payload, additional_metadata=additional_metadata or {})
-    return [event]
+    if not dry_run:
+        _get_hatchet().event.push(event, payload, additional_metadata=additional_metadata or {})
+    return [event]
""",
        },
        "expected_output": {
            "flags_security_issue": False,
            "verdict_is_positive": True,
            "mentions_keywords": ["dry_run", "testing", "useful"],
        },
    },
    {
        "input": {
            "repo": "amerenda/praetor",
            "pr_number": "995",
            "diff": """\
--- a/agents/coder/agent.py
+++ b/agents/coder/agent.py
@@ -15,6 +15,7 @@ GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
+API_KEY = "ghp_abc123secrettoken"

 def clone_repo(repo: str, token: str) -> str:
     path = f"/tmp/{repo.replace('/', '_')}"
""",
        },
        "expected_output": {
            "flags_security_issue": True,
            "mentions_keywords": ["secret", "hardcoded", "token", "credential"],
            "verdict": "REQUEST_CHANGES",
        },
    },
]


def ensure_dataset(lf: Langfuse, name: str, description: str, items: list[dict]) -> None:
    try:
        existing = lf.get_dataset(name)
        existing_count = len(existing.items)
        if existing_count >= len(items):
            print(f"  {name}: already has {existing_count} items — skipping")
            return
        print(f"  {name}: has {existing_count} items, adding {len(items) - existing_count} more")
    except Exception:
        print(f"  {name}: creating dataset")
        lf.create_dataset(name=name, description=description)

    for i, item in enumerate(items):
        lf.create_dataset_item(
            dataset_name=name,
            input=item["input"],
            expected_output=item["expected_output"],
        )
        print(f"    [{i+1}/{len(items)}] item created")


def main() -> None:
    lf = _client()
    print("Creating eval datasets in Langfuse...")

    ensure_dataset(
        lf,
        name="research-eval",
        description="Research agent eval: 5 research topics with keyword-based scoring criteria.",
        items=RESEARCH_ITEMS,
    )
    ensure_dataset(
        lf,
        name="reviewer-eval",
        description="PR reviewer agent eval: 5 synthetic diffs with security and quality criteria.",
        items=REVIEWER_ITEMS,
    )

    print("Done.")


if __name__ == "__main__":
    main()
