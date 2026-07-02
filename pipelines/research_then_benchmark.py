"""Pipeline nodes: ResearchNode → BenchmarkNode for Phase 29 model evaluation.

ResearchNode dispatches the research agent to gather model info from HuggingFace/Ollama.
BenchmarkNode runs the 4-category capability suite and writes scores to Langfuse + Mem0.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

from agents.model_benchmark.bench import run_all, RUNNERS
from common.memory_tools import add_memory, search_memory

logger = logging.getLogger(__name__)

_LANGFUSE_HOST    = os.environ.get("LANGFUSE_HOST", "")
_LANGFUSE_PUB     = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
_LANGFUSE_SECRET  = os.environ.get("LANGFUSE_SECRET_KEY", "")


@dataclass
class ModelEvalState:
    model:   str
    runner:  str
    task_id: int
    quant:   str = ""


# ── Research node ──────────────────────────────────────────────────────────────

_RESEARCH_PROMPT_TMPL = """\
Research the model "{model}" for local deployment on {runner}:

1. Confirm the parameter count and architecture (MoE vs dense, MLA vs GQA).
2. Find all available quantization options (GGUF, Ollama tags, AWQ, GPTQ).
   Recommended quantization for {runner} ({vram_gb} GB VRAM/memory): {recommended_quant}.
3. Find the recommended context length and any rope scaling / RoPE theta values.
4. Find known issues or notes about tool calling / function calling support for this model family.
5. Find optimal runtime parameters:
   - Ollama: num_ctx, num_gpu, num_thread, rope_frequency_base
   - llama.cpp: --n-ctx, --rope-scaling, --rope-freq-base, --n-gpu-layers
6. Check if this model is available via `ollama pull {model}` or requires a manual GGUF download.
7. Find any benchmark scores (MMLU, HumanEval, MATH) from the model card or leaderboards.

Return a structured summary with these exact fields:
params_B, recommended_quant, context_length, tool_calling_notes, optimal_flags, ollama_available, known_issues
"""

_RUNNER_VRAM = {
    "archlinux": {"vram_gb": 16,  "recommended_quant": "Q4_K_M"},
    "mac-mini":  {"vram_gb": 8,   "recommended_quant": "Q4_K_M"},
    "murderbot": {"vram_gb": 24,  "recommended_quant": "Q8_0"},
}


@dataclass
class ResearchNode:
    async def run(self, state: ModelEvalState) -> dict:
        mem_key = f"model-eval-{state.model.replace(':', '-')}"
        existing = await search_memory(state.model, "model-factory")
        if existing:
            logger.info("ResearchNode: cache hit for %s", state.model)
            return {"model": state.model, "summary": "\n".join(existing[:5]), "skipped": True}

        runner_info = _RUNNER_VRAM.get(state.runner, {"vram_gb": "?", "recommended_quant": "Q4_K_M"})
        prompt = _RESEARCH_PROMPT_TMPL.format(
            model=state.model,
            runner=state.runner,
            vram_gb=runner_info["vram_gb"],
            recommended_quant=state.quant or runner_info["recommended_quant"],
        )

        from agents.research.agent import build_agent as build_research_agent
        agent = build_research_agent()
        result = await agent.run(
            f"Task #{state.task_id}: Research model {state.model} for deployment\n\n{prompt}"
        )
        summary = str(result.output)

        await add_memory(
            content=f"Model research [{state.model}]: {summary[:500]}",
            agent_id="model-factory",
        )
        return {"model": state.model, "summary": summary, "skipped": False}


# ── Benchmark node ─────────────────────────────────────────────────────────────

@dataclass
class BenchmarkNode:
    async def run(self, state: ModelEvalState, research: dict) -> dict:
        import asyncio
        loop = asyncio.get_running_loop()

        # run_all is sync (httpx calls) — offload to thread pool
        result = await loop.run_in_executor(
            None, lambda: run_all(model=state.model, runner=state.runner)
        )

        scores = result["scores"]
        supports_fn = result["supports_function_calling"]
        recommendation = result["recommendation"]

        report_lines = [
            f"Model: {state.model} @ {state.runner}",
            f"Tool calling:          {scores['tool_calling']:.2f}" + (" ✓" if supports_fn else " ✗"),
            f"Approval gating:       {scores['approval_gating']:.2f}",
            f"Instruction following: {scores['instruction_following']:.2f}",
            f"Capability:            {scores['capability']:.2f}",
            f"Composite:             {scores['composite']:.2f}",
            f"supports_function_calling: {str(supports_fn).lower()}",
            f"Recommendation: {recommendation}",
        ]
        report = "\n".join(report_lines)

        # Research context for the full memo
        research_summary = research.get("summary", "")
        memo = f"{report}\n\nResearch context:\n{research_summary[:600]}"

        await add_memory(
            content=f"Benchmark result [{state.model} @ {state.runner}]: {memo[:800]}",
            agent_id="model-factory",
        )
        await add_memory(
            content=f"Model {state.model}: composite={scores['composite']:.2f}, fn_calling={supports_fn}, {recommendation}",
            agent_id="planner-global",
        )

        _write_langfuse(state.model, state.runner, scores, result)

        return {
            "model":      state.model,
            "runner":     state.runner,
            "scores":     scores,
            "supports_function_calling": supports_fn,
            "recommendation":            recommendation,
            "report":                    report,
        }


def _write_langfuse(model: str, runner: str, scores: dict, result: dict) -> None:
    if not (_LANGFUSE_HOST and _LANGFUSE_PUB and _LANGFUSE_SECRET):
        return
    try:
        from langfuse import Langfuse
        lf = Langfuse(host=_LANGFUSE_HOST, public_key=_LANGFUSE_PUB, secret_key=_LANGFUSE_SECRET)
        trace = lf.trace(
            name=f"model-benchmark-{model.replace(':', '-')}",
            tags=["model-factory", "benchmark", runner],
            metadata={"model": model, "runner": runner},
        )
        for cat, score in scores.items():
            trace.score(name=f"benchmark-{cat}", value=score)
        lf.flush()
        logger.info("Langfuse trace written: %s", trace.id)
    except Exception as exc:
        logger.warning("Langfuse write failed: %s", exc)
