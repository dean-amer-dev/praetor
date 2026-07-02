"""Model benchmark CLI — Phase 29.

Runs the 4-category capability suite directly against a runner (no Hatchet).
Use this for quick local testing; production runs go through POST /api/v1/model/benchmark.

Usage:
    python scripts/run_model_benchmark.py --model qwen3:14b
    python scripts/run_model_benchmark.py --model qwen3:30b --runner archlinux --verbose
    python scripts/run_model_benchmark.py --model qwen3-35b  --runner murderbot --no-langfuse
    python scripts/run_model_benchmark.py --model qwen3:8b   --skip capability

Runners: archlinux (default), mac-mini, murderbot

Environment (optional):
    LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY
    RUNNER_URL   — override the runner's default base URL
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# Allow running from the repo root without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.model_benchmark.bench import (
    RUNNERS,
    run_capability,
    run_approval_gating,
    run_instruction_following,
    run_tool_calling,
    TOOL_CALLING_ITEMS,
    APPROVAL_ITEMS,
    INSTRUCTION_ITEMS,
    CAPABILITY_ITEMS,
)


def _write_langfuse(model: str, runner: str, scores: dict[str, float], run_name: str) -> None:
    try:
        from langfuse import Langfuse
        lf = Langfuse(
            host=os.environ["LANGFUSE_HOST"],
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        )
        trace = lf.trace(
            name=f"model-benchmark-{model.replace(':', '-')}",
            tags=["model-factory", "benchmark", runner],
            metadata={"model": model, "runner": runner, "run_name": run_name},
        )
        for category, score in scores.items():
            trace.score(name=f"benchmark-{category}", value=score)
        composite = sum(scores.values()) / len(scores)
        trace.score(name="benchmark-composite", value=composite)
        lf.flush()
        print(f"\n  Langfuse trace: {os.environ['LANGFUSE_HOST']}/traces/{trace.id}")
    except Exception as exc:
        print(f"\n  [warn] Langfuse write failed: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Praetor Phase 29 model benchmark suite")
    parser.add_argument("--model", required=True,
                        help="Model to benchmark, e.g. qwen3:14b or qwen3-35b")
    parser.add_argument("--runner", default="archlinux", choices=list(RUNNERS),
                        help="Runner to target")
    parser.add_argument("--no-langfuse", action="store_true",
                        help="Skip writing results to Langfuse")
    parser.add_argument("--skip", action="append", default=[],
                        choices=["tool-calling", "approval", "instruction", "capability"],
                        help="Skip a benchmark category (repeatable)")
    parser.add_argument("--run-name", default="",
                        help="Custom run name for Langfuse")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print per-item details")
    args = parser.parse_args()

    cfg = RUNNERS[args.runner]
    url = os.environ.get("RUNNER_URL") or cfg["url"]
    api_key = cfg["api_key"]
    model = args.model
    run_name = args.run_name or f"{model.replace(':', '-')}-{int(time.time())}"

    print(f"\nModel Benchmark — {model} @ {args.runner} ({url})")
    print(f"Run: {run_name}")
    print("=" * 65)

    scores: dict[str, float] = {}

    if "tool-calling" not in args.skip:
        print(f"\n[1/4] Tool Calling ({len(TOOL_CALLING_ITEMS)} prompts) ...", flush=True)
        t0 = time.monotonic()
        score, details = run_tool_calling(url, api_key, model)
        scores["tool_calling"] = score
        passed = sum(1 for r in details if r["passed"])
        print(f"      Score: {score:.2f}  ({passed}/{len(details)} passed)  [{time.monotonic()-t0:.0f}s]")
        if args.verbose:
            for r in details:
                mark = "✓" if r["passed"] else "✗"
                print(f"      {mark} {r['prompt']:<55}  called={r.get('called') or '-'}")
        note = " ✓ passes fn-calling threshold" if score >= 0.7 else " ✗ below 0.7 (supports_function_calling=false)"
        print(f"      {note}")

    if "approval" not in args.skip:
        print(f"\n[2/4] Approval Gating ({len(APPROVAL_ITEMS)} prompts) ...", flush=True)
        t0 = time.monotonic()
        score, details = run_approval_gating(url, api_key, model)
        scores["approval_gating"] = score
        passed = sum(1 for r in details if r["passed"])
        print(f"      Score: {score:.2f}  ({passed}/{len(details)} passed)  [{time.monotonic()-t0:.0f}s]")
        if args.verbose:
            for r in details:
                mark = "✓" if r["passed"] else "✗"
                dtype = "destructive" if r["destructive"] else "safe"
                print(f"      {mark} [{dtype}] {r['prompt']:<50}  asked={r.get('asked_confirmation')}")

    if "instruction" not in args.skip:
        print(f"\n[3/4] Instruction Following ({len(INSTRUCTION_ITEMS)} prompts) ...", flush=True)
        t0 = time.monotonic()
        score, details = run_instruction_following(url, api_key, model)
        scores["instruction_following"] = score
        passed = sum(1 for r in details if r["passed"])
        print(f"      Score: {score:.2f}  ({passed}/{len(details)} passed)  [{time.monotonic()-t0:.0f}s]")
        if args.verbose:
            for r in details:
                mark = "✓" if r["passed"] else "✗"
                print(f"      {mark} [{r['desc']}] {r['prompt']:<50}")
                if not r["passed"] and r.get("preview"):
                    print(f"           got: {r['preview']}")

    if "capability" not in args.skip:
        print(f"\n[4/4] General Capability ({len(CAPABILITY_ITEMS)} prompts) ...", flush=True)
        t0 = time.monotonic()
        score, details = run_capability(url, api_key, model)
        scores["capability"] = score
        print(f"      Score: {score:.2f}  [{time.monotonic()-t0:.0f}s]")
        if args.verbose:
            for r in details:
                print(f"      {r['score']:.2f}  {r['prompt']:<55}  ({r.get('reason', '')})")

    print("\n" + "=" * 65)
    print(f"SCORECARD — {model} @ {args.runner}")
    print("=" * 65)
    for cat, score in scores.items():
        bar = "█" * int(score * 20)
        print(f"  {cat:<26} {score:.2f}  {bar}")
    if scores:
        composite = sum(scores.values()) / len(scores)
        print(f"  {'composite':<26} {composite:.2f}")
        supports_fn = scores.get("tool_calling", 0.0) >= 0.7
        print(f"\n  supports_function_calling: {'true ✓' if supports_fn else 'false ✗'}")
        print(f"  recommendation:            {'REGISTER' if composite >= 0.6 else 'DO NOT REGISTER'}")

    if not args.no_langfuse and os.environ.get("LANGFUSE_HOST"):
        _write_langfuse(model, args.runner, scores, run_name)
    elif not args.no_langfuse:
        print("\n  [info] Set LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY to write results to Langfuse.")

    print()


if __name__ == "__main__":
    main()
