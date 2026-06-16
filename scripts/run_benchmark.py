"""Benchmark runner: dispatch agent:benchmark events for a full dataset and print results.

Usage:
    python scripts/run_benchmark.py --dataset research-eval --agent-type research
    python scripts/run_benchmark.py --dataset reviewer-eval --agent-type review

Results are written to Langfuse as a named experiment and printed to stdout.
Requires: LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LITELLM_BASE_URL,
          LITELLM_API_KEY, HATCHET_CLIENT_TOKEN all set in the environment.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

from hatchet_sdk import Hatchet
from langfuse import Langfuse


def _require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


def _poll_hatchet_run(hatchet: Hatchet, run_id: str, timeout: int = 900) -> dict | None:
    """Poll a Hatchet workflow run until terminal state. Returns the run or None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            run = hatchet.runs.get(run_id)
            if run.status in ("SUCCEEDED", "FAILED", "CANCELLED"):
                return run
        except Exception:
            pass
        time.sleep(10)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Praetor agent benchmark suite")
    parser.add_argument("--dataset", required=True, help="Langfuse dataset name, e.g. research-eval")
    parser.add_argument("--agent-type", required=True, choices=["research", "review"],
                        help="Agent type to benchmark")
    parser.add_argument("--model", default="qwen3-35b", help="LiteLLM model alias")
    parser.add_argument("--prompt-version", default="", help="Langfuse prompt name:version, e.g. research-system:v2")
    parser.add_argument("--run-name", default="", help="Experiment name override")
    args = parser.parse_args()

    _require_env(
        "LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
        "HATCHET_CLIENT_TOKEN",
    )

    lf = Langfuse(
        host=os.environ["LANGFUSE_HOST"],
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
    )
    hatchet = Hatchet()

    run_name = args.run_name or f"{args.dataset}-{datetime.utcnow().strftime('%Y-%m-%d')}"

    try:
        dataset = lf.get_dataset(args.dataset)
    except Exception as exc:
        print(f"ERROR: could not fetch dataset '{args.dataset}': {exc}", file=sys.stderr)
        print("Run: python scripts/create_eval_datasets.py", file=sys.stderr)
        sys.exit(1)

    items = dataset.items
    if not items:
        print(f"ERROR: dataset '{args.dataset}' has no items", file=sys.stderr)
        sys.exit(1)

    print(f"Running benchmark: {args.dataset} ({len(items)} items) × {args.model} × {args.prompt_version or 'default'}")
    print(f"Experiment name: {run_name}")
    print()

    scores: list[float] = []
    for i, item in enumerate(items, 1):
        label = str(item.input.get("task_title") or item.input.get("diff", "")[:40] or item.id)
        print(f"[{i}/{len(items)}] {label[:50]:<50} ", end="", flush=True)

        # Dispatch benchmark via Hatchet event
        hatchet.event.push("agent:benchmark", {
            "dataset_name": args.dataset,
            "dataset_item_id": item.id,
            "agent_type": args.agent_type,
            "model": args.model,
            "prompt_version": args.prompt_version,
            "run_name": run_name,
        })

        print("dispatched — waiting...", flush=True)

    print()
    print(f"All {len(items)} benchmark tasks dispatched.")
    print(f"View results at: {os.environ['LANGFUSE_HOST']}/datasets/{args.dataset}/runs")
    print()
    print("Note: scores appear in Langfuse as the benchmark-worker completes each run.")
    print(f"Expected completion: ~{len(items) * 3} minutes")


if __name__ == "__main__":
    main()
