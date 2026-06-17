# Agent Eval Baselines

Baseline scores recorded after Phase 13 first benchmark run.

Any future prompt change must be benchmarked before promoting to production.
If mean score drops more than 0.05 below baseline, the change is rejected.

## How to update

```bash
# Run benchmark and record results here after comparing to current baseline
python scripts/run_benchmark.py --dataset research-eval --agent-type research
python scripts/run_benchmark.py --dataset reviewer-eval --agent-type review
```

## Baselines

| Dataset | Model | Prompt | Date | Mean Score | Min Score |
|---------|-------|--------|------|-----------|-----------|
| research-eval | qwen3-35b | default | 2026-06-17 | 1.000 | 1.000 |
| reviewer-eval | qwen3-35b | default | 2026-06-17 | 0.846 | 0.330 |

Run name: `baseline-1781706219` (5 items each dataset, scored by LLM judge)
