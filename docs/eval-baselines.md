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
| *(to be populated after first benchmark run)* | | | | | |
