"""evals.py — evaluation harness over historical failing runs.

The corpus is synthetic and seeded (no private data): each historical run
pairs a rendered failure log with its known ground-truth root cause.
Metrics: top-1 ranking accuracy and median time-to-first-hypothesis
(wall-clock time for the agent to emit its ranked list, whose first entry
is the first hypothesis — measured, not simulated).
"""
from __future__ import annotations

import random
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from .ingest import ingest_case
from .triage import LLMProvider, MockProvider, TriageAgent

_TEMPLATES: dict[str, list[str]] = {
    "null_reference": [
        "AttributeError: 'NoneType' object has no attribute 'id'\n  File \"app/checkout.py\", line 42, in pay",
        "TypeError: cannot read properties of null (reading 'total')",
    ],
    "timeout": [
        "ReadTimeout: HTTPSConnectionPool timeout: timed out after 5s",
        "AssertionError: deadline exceeded waiting for checkout response",
    ],
    "assertion_mismatch": [
        "AssertionError: assert 42 == 43  # expected total mismatch",
        "AssertionError: expected 'USD' != 'EUR'",
    ],
    "missing_fixture": [
        "FileNotFoundError: seed fixture orders.json does not exist",
        "KeyError: 'user-99' — fixture not found in seeded db",
    ],
    "race_condition": [
        "Flaky failure: intermittent race — stale element after concurrent update",
        "AssertionError: eventually consistent value not visible (order-dependent)",
    ],
    "config_error": [
        "Connection refused: missing setting DATABASE_URL, invalid configuration",
        "Environment variable CHECKOUT_URL not set — config error",
    ],
    "dependency_failure": [
        "ModuleNotFoundError: No module named 'payments_sdk' — dependency failure",
        "ImportError: incompatible dependency version 1.x vs required 2.x",
    ],
    "permission_error": [
        "HTTP 403 Forbidden: permission denied for test identity",
        "401 Unauthorized: access denied calling billing API",
    ],
}


@dataclass
class HistoricalRun:
    run_id: str
    test_id: str
    suite: str
    log: str
    ground_truth: str


@dataclass
class EvalResult:
    total: int
    top1_correct: int
    top1_accuracy: float
    median_ttfh_ms: float
    per_category: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total, "top1_correct": self.top1_correct,
            "top1_accuracy": round(self.top1_accuracy, 4),
            "median_time_to_first_hypothesis_ms": round(self.median_ttfh_ms, 3),
            "per_category": self.per_category,
        }


def generate_corpus(n: int = 50, seed: int = 42) -> list[HistoricalRun]:
    if n <= 0:
        raise ValueError("n must be positive")
    rng = random.Random(seed)
    cats = sorted(_TEMPLATES)
    runs = []
    for i in range(n):
        cat = cats[i % len(cats)] if i < len(cats) else rng.choice(cats)
        log = rng.choice(_TEMPLATES[cat])
        runs.append(HistoricalRun(
            run_id=f"run-{i:04d}", test_id=f"test_{cat}_{i}", suite=rng.choice(["checkout", "auth", "billing"]),
            log=log, ground_truth=cat,
        ))
    return runs


def evaluate(provider: LLMProvider | None = None, corpus: list[HistoricalRun] | None = None,
             n: int = 50, seed: int = 42) -> EvalResult:
    corpus = corpus if corpus is not None else generate_corpus(n=n, seed=seed)
    agent = TriageAgent(provider or MockProvider())
    correct, latencies = 0, []
    per: dict[str, dict[str, int]] = {}
    for run in corpus:
        case = ingest_case(test_id=run.test_id, suite=run.suite, log=run.log, stack_trace=run.log)
        start = time.perf_counter()
        result = agent.triage(case)
        latencies.append((time.perf_counter() - start) * 1000.0)
        top1 = result.hypotheses[0].category if result.hypotheses else ""
        ok = top1 == run.ground_truth
        correct += int(ok)
        bucket = per.setdefault(run.ground_truth, {"total": 0, "correct": 0})
        bucket["total"] += 1
        bucket["correct"] += int(ok)
    per_out = {k: {**v, "accuracy": round(v["correct"] / v["total"], 4) if v["total"] else 0.0} for k, v in sorted(per.items())}
    return EvalResult(
        total=len(corpus), top1_correct=correct,
        top1_accuracy=(correct / len(corpus)) if corpus else 0.0,
        median_ttfh_ms=statistics.median(latencies) if latencies else 0.0,
        per_category=per_out,
    )
