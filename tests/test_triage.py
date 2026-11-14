import json
import pytest
from testpilot.ingest import ingest_case, ingest_directory, parse_diff, parse_stack_trace, redact_secrets
from testpilot.triage import (
    GuardrailError, MockProvider, TriageAgent, call_tool, get_tool_definitions, validate_result,
)

LOG = "Traceback\n  File \"app/checkout.py\", line 42, in pay\nAttributeError: 'NoneType' object has no attribute 'id'"
DIFF = "diff --git a/app/checkout.py b/app/checkout.py\n@@ -40,3 +40,3 @@\n-old\n+new\n"


def test_ingest_parses_stack_and_diff():
    case = ingest_case(test_id="test_pay", suite="checkout", log=LOG, stack_trace=LOG, diff=DIFF)
    assert case.exception_type == "AttributeError"
    assert case.frames[0].line == 42 and case.frames[0].function == "pay"
    assert case.changed_files == ["app/checkout.py"]
    assert case.error_lines


def test_ingest_redacts_secrets():
    case = ingest_case(test_id="t", log="password=hunter2 token: abc123 failed")
    assert "hunter2" not in case.log and "***" in redact_secrets("api_key=xyz")


def test_parse_helpers():
    frames, t, _ = parse_stack_trace(LOG)
    assert frames and t == "AttributeError"
    files, hunks = parse_diff(DIFF)
    assert files == ["app/checkout.py"] and hunks == 1


def test_ingest_directory(tmp_path):
    (tmp_path / "run.log").write_text(LOG)
    (tmp_path / "change.diff").write_text(DIFF)
    case = ingest_directory(tmp_path, test_id="test_pay")
    assert case.test_id == "test_pay" and case.changed_files


def test_ingest_requires_test_id():
    with pytest.raises(ValueError):
        ingest_case(test_id="  ", log="x")


# --- triage schema / guardrails / determinism ---
def _case():
    return ingest_case(test_id="test_pay", suite="checkout", log=LOG, stack_trace=LOG, diff=DIFF)


def test_triage_output_schema():
    r = TriageAgent(MockProvider()).triage(_case())
    d = r.to_dict()
    assert d["hypotheses"][0]["category"] == "null_reference"
    assert all(0 <= h["confidence"] <= 1 for h in d["hypotheses"])
    confs = [h["confidence"] for h in d["hypotheses"]]
    assert confs == sorted(confs, reverse=True)
    for f in ("title", "repro", "suspected_cause", "suggested_fix"):
        assert d["bug_report"][f]


def test_mock_provider_deterministic():
    a, b = MockProvider(), MockProvider()
    payload = json.dumps(_case().to_dict(), sort_keys=True, default=str)
    assert a.complete("s", payload) == b.complete("s", payload)
    r1 = TriageAgent(MockProvider()).triage(_case()).to_dict()
    r2 = TriageAgent(MockProvider()).triage(_case()).to_dict()
    r1.pop("latency_ms"); r2.pop("latency_ms")
    assert r1 == r2


def test_guardrail_empty_case():
    with pytest.raises(GuardrailError):
        TriageAgent(MockProvider()).triage(ingest_case(test_id="t"))


def test_guardrail_unknown_category_and_unsorted():
    bug = {"title": "t", "repro": "r", "suspected_cause": "c", "suggested_fix": "f"}
    with pytest.raises(GuardrailError):
        validate_result({"hypotheses": [{"category": "nope", "confidence": 0.5, "rationale": "x"}], "bug_report": bug})
    with pytest.raises(GuardrailError):
        validate_result({"hypotheses": [
            {"category": "timeout", "confidence": 0.2, "rationale": "x"},
            {"category": "null_reference", "confidence": 0.9, "rationale": "y"},
        ], "bug_report": bug})
    with pytest.raises(GuardrailError):
        validate_result({"hypotheses": [{"category": "timeout", "confidence": 1.5, "rationale": "x"}], "bug_report": bug})
    with pytest.raises(GuardrailError):
        validate_result({"hypotheses": [{"category": "timeout", "confidence": 0.5, "rationale": "x"}], "bug_report": {"title": "t"}})


def test_mcp_tools_shape_and_dispatch():
    tools = get_tool_definitions()
    assert {t["name"] for t in tools} == {"triage_failure", "rank_hypotheses", "draft_bug_report"}
    assert all("inputSchema" in t for t in tools)
    out = call_tool("triage_failure", {"test_id": "test_pay", "log": LOG, "stack_trace": LOG})
    assert out["bug_report"]["title"]
    assert call_tool("rank_hypotheses", {"test_id": "t", "log": LOG})["hypotheses"]
    assert call_tool("draft_bug_report", {"test_id": "t", "log": LOG})["bug_report"]
    with pytest.raises(GuardrailError):
        call_tool("nope", {})
