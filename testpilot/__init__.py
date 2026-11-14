"""testpilot — agentic triage for test failures."""
from .ingest import FailureCase, ingest_case, parse_diff, parse_log, parse_stack_trace
from .triage import TriageAgent, MockProvider, TriageResult

__version__ = "0.1.0"
__all__ = [
    "FailureCase", "ingest_case", "parse_diff", "parse_log", "parse_stack_trace",
    "TriageAgent", "MockProvider", "TriageResult", "__version__",
]
