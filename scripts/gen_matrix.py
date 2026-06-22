"""Generate the build matrix JSON for GitHub Actions detect-changes step.

Usage: python3 scripts/gen_matrix.py '<rebuild_json>'
Output: JSON {"include": [...]} for the build-amd64 matrix strategy.
"""
import sys
import json
import glob
import os

STATIC: dict[str, dict] = {
    "coder":     {"component": "coder",     "image": "amerenda/praetor-coder",     "dockerfile": "Dockerfile.coder-worker"},
    "pipeline":  {"component": "pipeline",  "image": "amerenda/praetor-pipeline",  "dockerfile": "Dockerfile.pipeline-worker"},
    "research":  {"component": "research",  "image": "amerenda/praetor-research",  "dockerfile": "Dockerfile.research-worker"},
    "webhook":   {"component": "webhook",   "image": "amerenda/praetor-webhook",   "dockerfile": "Dockerfile.webhook-adapter"},
    "qa":        {"component": "qa",        "image": "amerenda/praetor-qa",        "dockerfile": "Dockerfile.qa-worker"},
    "reviewer":  {"component": "reviewer",  "image": "amerenda/praetor-reviewer",  "dockerfile": "Dockerfile.reviewer-worker"},
    "benchmark": {"component": "benchmark", "image": "amerenda/praetor-benchmark", "dockerfile": "Dockerfile.benchmark-worker"},
}

rebuild: list[str] = json.loads(sys.argv[1])
matrix = dict(STATIC)
for df in glob.glob("agents/*/Dockerfile"):
    name = os.path.basename(os.path.dirname(df))
    if name not in matrix:
        matrix[name] = {"component": name, "image": f"amerenda/praetor-{name}", "dockerfile": df}

print(json.dumps({"include": [matrix[x] for x in rebuild if x in matrix]}))
