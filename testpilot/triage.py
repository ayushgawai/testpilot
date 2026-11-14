"""triage.py — agentic triage: ranked root-cause hypotheses + drafted bug report.

The LLM sits behind :class:`LLMProvider` (a Protocol).  :class:`MockProvider`
is deterministic — same case in, byte-identical JSON out — so tests, evals
and the bench need no network or API key.  A real provider only has to
implement ``complete(system, user) -> str`` returning the JSON schema below.

Guardrails (enforced in :func:`validate_result`, regardless of provider):
  * at most ``MAX_HYPOTHESES`` hypotheses, confidences in [0, 1], sorted desc
  * every hypothesis names a known root-cause category and cites evidence
  * bug report must contain title / repro / suspected_cause / suggested_fix
  * case text is secret-redacted and length-capped before it reaches a provider

MCP-style surface: :data:`TOOLS` is a list of ``{name, description,
inputSchema}`` dicts and :func:`call_tool` dispatches by name, mirroring the
shape an MCP client expects without taking an MCP SDK dependency.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

from .ingest import FailureCase, ingest_case, redact_secrets

MAX_HYPOTHESES = 5
MAX_CASE_CHARS = 20_000

ROOT_CAUSES: dict[str, dict[str, Any]] = {
    "null_reference": {
        "signals": ["none", "null", "nonetype", "attributeerror", "nullpointer", "cannot read propert", "is not defined"],
        "fix": "Guard the None/null return at the call site and make the producer return an explicit empty value or raise a domain error.",
    },
    "timeout": {
        "signals": ["timeout", "timed out", "deadline exceeded", "readtimeout", "connecttimeout"],
        "fix": "Raise the deadline only if the call is genuinely slow; otherwise add caching/retries with backoff and surface the slow dependency.",
    },
    "assertion_mismatch": {
        "signals": ["assertionerror", "assert ", "expected", "mismatch", "!= ", "not equal"],
        "fix": "Compare expected vs actual in the failing assertion; fix the regressed value or update the stale expectation with justification.",
    },
    "missing_fixture": {
        "signals": ["fixture", "no such table", "does not exist", "not found", "filenotfound", "keyerror", "seed"],
        "fix": "Seed the missing fixture/data before the test (see data_setup.seed_fixture_env) and assert it exists in setup.",
    },
    "race_condition": {
        "signals": ["race", "flaky", "intermittent", "concurrent", "eventually", "stale element", "order-dependent"],
        "fix": "Remove the ordering assumption: wait on a condition instead of sleeping, and isolate shared state per test.",
    },
    "config_error": {
        "signals": ["config", "environment variable", "env var", "missing setting", "invalid configuration", "connection refused"],
        "fix": "Provide the missing/invalid setting in the test environment and validate configuration at startup with a clear error.",
    },
    "dependency_failure": {
        "signals": ["importerror", "modulenotfound", "no module named", "version", "incompatible", "dependency"],
        "fix": "Pin/install the missing or incompatible dependency and lock the version used in CI.",
    },
    "permission_error": {
        "signals": ["permission", "forbidden", "401", "403", "unauthorized", "access denied"],
        "fix": "Grant the test identity the required permission or mock the authorized call; do not broaden production permissions for a test.",
    },
}


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    def complete(self, system: str, user: str) -> str: ...


SYSTEM_PROMPT = (
    "You are a test-failure triage agent. Return ONLY JSON with keys "
    "hypotheses (list of {category, confidence, rationale, evidence}) and "
    "bug_report ({title, repro, suspected_cause, suggested_fix}). "
    "Categories must come from: " + ", ".join(sorted(ROOT_CAUSES))
)


class MockProvider:
    """Deterministic keyword-signal provider. No randomness, no clock, no I/O."""

    name = "mock"

    def complete(self, system: str, user: str) -> str:
        case = json.loads(user) if user.strip().startswith("{") else {"log": user}
        text = " ".join(str(case.get(k, "")) for k in ("log", "stack_trace", "diff", "exception_type", "exception_message")).lower()
        scored: list[tuple[str, float, list[str]]] = []
        for category, spec in ROOT_CAUSES.items():
            hits = [s for s in spec["signals"] if s in text]
            if hits:
                # Deterministic score: signal count + longest-signal bonus.
                scored.append((category, round(0.35 + 0.15 * len(hits) + 0.01 * max(len(h) for h in hits), 4), hits))
        if not scored:
            scored = [("assertion_mismatch", 0.30, ["no strong signal; defaulting to assertion mismatch"])]
        scored.sort(key=lambda x: (-x[1], x[0]))
        total = sum(s for _, s, _ in scored) or 1.0
        hyps = [
            {
                "category": cat,
                "confidence": round(min(0.99, s / total), 4),
                "rationale": f"Log/stack/diff contains signal(s): {', '.join(hits[:3])}",
                "evidence": hits[:3],
            }
            for cat, s, hits in scored[:MAX_HYPOTHESES]
        ]
        # Re-sort after normalisation so confidences are strictly descending-ish.
        hyps.sort(key=lambda h: (-h["confidence"], h["category"]))
        top = hyps[0]["category"]
        test_id = case.get("test_id", "unknown")
        bug = {
            "title": f"[testpilot] {test_id}: suspected {top.replace('_', ' ')}",
            "repro": f"Run {test_id} with the captured log/stack/diff; it fails deterministically with the recorded exception.",
            "suspected_cause": f"{top}: {hyps[0]['rationale']}",
            "suggested_fix": ROOT_CAUSES[top]["fix"],
        }
        return json.dumps({"hypotheses": hyps, "bug_report": bug}, sort_keys=True)


@dataclass
class Hypothesis:
    category: str
    confidence: float
    rationale: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class BugReport:
    title: str
    repro: str
    suspected_cause: str
    suggested_fix: str


@dataclass
class TriageResult:
    test_id: str
    hypotheses: list[Hypothesis]
    bug_report: BugReport
    provider: str = "mock"
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GuardrailError(ValueError):
    pass


def validate_result(data: dict[str, Any], test_id: str = "") -> TriageResult:
    if not isinstance(data, dict):
        raise GuardrailError("provider output must be a JSON object")
    hyps_raw = data.get("hypotheses")
    if not hyps_raw or not isinstance(hyps_raw, list):
        raise GuardrailError("at least one hypothesis is required")
    if len(hyps_raw) > MAX_HYPOTHESES:
        raise GuardrailError(f"too many hypotheses: {len(hyps_raw)} > {MAX_HYPOTHESES}")
    hyps: list[Hypothesis] = []
    for h in hyps_raw:
        cat = h.get("category", "")
        if cat not in ROOT_CAUSES:
            raise GuardrailError(f"unknown root-cause category: {cat!r}")
        conf = float(h.get("confidence", -1))
        if not 0.0 <= conf <= 1.0:
            raise GuardrailError(f"confidence out of [0,1]: {conf}")
        if not h.get("rationale"):
            raise GuardrailError("hypothesis rationale must be non-empty")
        hyps.append(Hypothesis(cat, conf, str(h["rationale"]), list(h.get("evidence", []))))
    if [h.confidence for h in hyps] != sorted((h.confidence for h in hyps), reverse=True):
        raise GuardrailError("hypotheses must be sorted by descending confidence")
    bug = data.get("bug_report") or {}
    for f in ("title", "repro", "suspected_cause", "suggested_fix"):
        if not str(bug.get(f, "")).strip():
            raise GuardrailError(f"bug_report.{f} must be non-empty")
    return TriageResult(
        test_id=test_id or str(data.get("test_id", "unknown")),
        hypotheses=hyps,
        bug_report=BugReport(**{f: str(bug[f]) for f in ("title", "repro", "suspected_cause", "suggested_fix")}),
    )


class TriageAgent:
    def __init__(self, provider: LLMProvider | None = None):
        self.provider = provider or MockProvider()

    def triage(self, case: FailureCase) -> TriageResult:
        if not (case.log or case.stack_trace):
            raise GuardrailError("refusing to triage a case with no log or stack trace")
        payload = case.to_dict()
        payload["log"] = redact_secrets(str(payload.get("log", ""))[:MAX_CASE_CHARS])
        payload["stack_trace"] = redact_secrets(str(payload.get("stack_trace", ""))[:MAX_CASE_CHARS])
        start = time.perf_counter()
        raw = self.provider.complete(SYSTEM_PROMPT, json.dumps(payload, sort_keys=True, default=str))
        latency = (time.perf_counter() - start) * 1000.0
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise GuardrailError(f"provider returned non-JSON output: {e}") from e
        result = validate_result(data, test_id=case.test_id)
        result.provider = getattr(self.provider, "name", type(self.provider).__name__)
        result.latency_ms = round(latency, 3)
        return result


# ---------------------------------------------------------------- MCP-style tools
TOOLS: list[dict[str, Any]] = [
    {
        "name": "triage_failure",
        "description": "Rank root-cause hypotheses and draft a bug report for a test failure.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "test_id": {"type": "string"}, "suite": {"type": "string"},
                "log": {"type": "string"}, "stack_trace": {"type": "string"}, "diff": {"type": "string"},
            },
            "required": ["test_id"],
        },
    },
    {
        "name": "rank_hypotheses",
        "description": "Return only the ranked hypotheses for a failure case.",
        "inputSchema": {
            "type": "object",
            "properties": {"test_id": {"type": "string"}, "log": {"type": "string"}, "stack_trace": {"type": "string"}, "diff": {"type": "string"}},
            "required": ["test_id"],
        },
    },
    {
        "name": "draft_bug_report",
        "description": "Return only the drafted bug report for a failure case.",
        "inputSchema": {
            "type": "object",
            "properties": {"test_id": {"type": "string"}, "log": {"type": "string"}, "stack_trace": {"type": "string"}, "diff": {"type": "string"}},
            "required": ["test_id"],
        },
    },
]


def get_tool_definitions() -> list[dict[str, Any]]:
    return json.loads(json.dumps(TOOLS))


def call_tool(name: str, arguments: dict[str, Any], provider: LLMProvider | None = None) -> dict[str, Any]:
    if name not in {t["name"] for t in TOOLS}:
        raise GuardrailError(f"unknown tool: {name!r}")
    args = dict(arguments or {})
    case = ingest_case(
        test_id=args.get("test_id", ""), suite=args.get("suite", "unknown"),
        log=args.get("log", ""), stack_trace=args.get("stack_trace", ""), diff=args.get("diff", ""),
    )
    result = TriageAgent(provider).triage(case)
    if name == "rank_hypotheses":
        return {"test_id": result.test_id, "hypotheses": [asdict(h) for h in result.hypotheses]}
    if name == "draft_bug_report":
        return {"test_id": result.test_id, "bug_report": asdict(result.bug_report)}
    return result.to_dict()
