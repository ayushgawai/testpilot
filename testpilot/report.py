"""report.py — pass rates and blockers per test suite."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable


def summarize_runs(runs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Each run: {suite, test_id, passed: bool, root_cause?: str, duration_ms?: float}."""
    by_suite: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        grouped[str(r.get("suite", "unknown"))].append(r)
    for suite, items in sorted(grouped.items()):
        total = len(items)
        passed = sum(1 for r in items if r.get("passed"))
        failures = [r for r in items if not r.get("passed")]
        causes = Counter(str(r.get("root_cause", "unknown")) for r in failures)
        failing_tests = Counter(str(r.get("test_id", "unknown")) for r in failures)
        blockers = [
            {"test_id": t, "failures": n} for t, n in failing_tests.most_common(5)
        ]
        by_suite[suite] = {
            "suite": suite, "total": total, "passed": passed, "failed": total - passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "top_root_causes": [{"category": c, "count": n} for c, n in causes.most_common(5)],
            "blockers": blockers,
        }
    overall_total = sum(s["total"] for s in by_suite.values())
    overall_passed = sum(s["passed"] for s in by_suite.values())
    return {
        "suites": by_suite,
        "overall": {
            "total": overall_total, "passed": overall_passed,
            "pass_rate": round(overall_passed / overall_total, 4) if overall_total else 0.0,
        },
    }


def render_markdown(summary: dict[str, Any]) -> str:
    lines = ["# Test suite report", "",
             f"Overall: {summary['overall']['passed']}/{summary['overall']['total']} passed "
             f"({summary['overall']['pass_rate']:.1%})", "",
             "| Suite | Total | Passed | Failed | Pass rate | Top blocker |",
             "|---|---:|---:|---:|---:|---|"]
    for s in summary["suites"].values():
        blocker = s["blockers"][0]["test_id"] if s["blockers"] else "—"
        lines.append(f"| {s['suite']} | {s['total']} | {s['passed']} | {s['failed']} | {s['pass_rate']:.1%} | {blocker} |")
    lines.append("")
    for s in summary["suites"].values():
        if s["top_root_causes"]:
            causes = ", ".join(f"{c['category']}×{c['count']}" for c in s["top_root_causes"])
            lines.append(f"- **{s['suite']}** blockers: {causes}")
    return "\n".join(lines) + "\n"
