"""bench.py — CLI bench over historical failing runs.

Usage:
    python -m testpilot.bench --runs 500
    python -m testpilot.bench --runs 50 --json

Reports root-cause top-1 rate and median time-to-first-hypothesis.
The full bench is 500 runs; scale down with --runs (the README shows a
50-run sanity run and says so).
"""
from __future__ import annotations

import argparse
import json
import sys

from .evals import evaluate, generate_corpus
from .triage import MockProvider


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="testpilot-bench", description="Benchmark testpilot triage over historical failing runs.")
    p.add_argument("--runs", type=int, default=500, help="number of historical failing runs (default 500)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = p.parse_args(argv)
    if args.runs <= 0:
        p.error("--runs must be positive")
    corpus = generate_corpus(n=args.runs, seed=args.seed)
    result = evaluate(provider=MockProvider(), corpus=corpus)
    d = result.to_dict()
    if args.json:
        print(json.dumps({"runs": args.runs, "seed": args.seed, "provider": "mock", **d}, indent=2, sort_keys=True))
    else:
        print(f"testpilot bench — provider=mock runs={args.runs} seed={args.seed}")
        print(f"  root-cause top-1 rate : {d['top1_accuracy']:.1%}  ({result.top1_correct}/{result.total})")
        print(f"  median time-to-first-hypothesis: {d['median_time_to_first_hypothesis_ms']:.3f} ms")
        print("  per-category top-1:")
        for cat, v in d["per_category"].items():
            print(f"    {cat:<22} {v['correct']}/{v['total']}  ({v['accuracy']:.1%})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
