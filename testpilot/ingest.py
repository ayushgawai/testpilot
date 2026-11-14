"""ingest.py — read test-failure artifacts into a structured case model.

Artifacts understood:
  * plain-text / structured logs (``*.log``, ``*.txt`` or a raw string)
  * Python-style stack traces embedded in a log or supplied separately
  * a unified-diff view of the recent change (``git diff`` output)

Everything is stdlib-only and deterministic: the same bytes always produce
the same :class:`FailureCase`.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_STACK_FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>\S+))?')
_EXCEPTION = re.compile(r"^(?P<type>[A-Za-z_][\w.]*?(?:Error|Exception|Failure|Timeout|Interrupt))\s*:\s*(?P<msg>.*)$", re.M)
_CHANGED_FILE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$", re.M)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.M)
_SECRET = re.compile(r"(?i)(password|passwd|api[_-]?key|secret|token)\s*[:=]\s*\S+")


def redact_secrets(text: str) -> str:
    """Replace obvious secret assignments with ``***`` (keeps the key name)."""
    return _SECRET.sub(lambda m: f"{m.group(1)}=***", text)


@dataclass
class StackFrame:
    file: str
    line: int
    function: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FailureCase:
    test_id: str
    suite: str = "unknown"
    log: str = ""
    stack_trace: str = ""
    diff: str = ""
    exception_type: str = ""
    exception_message: str = ""
    frames: list[StackFrame] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    error_lines: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FailureCase":
        frames = [StackFrame(**f) if isinstance(f, dict) else f for f in data.get("frames", [])]
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        known["frames"] = frames
        return cls(**known)


def parse_stack_trace(text: str) -> tuple[list[StackFrame], str, str]:
    frames = [
        StackFrame(file=m.group("file"), line=int(m.group("line")), function=m.group("func") or "")
        for m in _STACK_FRAME.finditer(text or "")
    ]
    exc_type, exc_msg = "", ""
    matches = list(_EXCEPTION.finditer(text or ""))
    if matches:
        exc_type, exc_msg = matches[-1].group("type"), matches[-1].group("msg").strip()
    return frames, exc_type, exc_msg


def parse_diff(diff: str) -> tuple[list[str], int]:
    files = [m.group("b") for m in _CHANGED_FILE.finditer(diff or "")]
    return files, len(_HUNK.findall(diff or ""))


def parse_log(log: str) -> list[str]:
    out = []
    for line in (log or "").splitlines():
        low = line.lower()
        if any(k in low for k in ("error", "fail", "assert", "exception", "timeout", "traceback")):
            out.append(line.strip()[:300])
    return out[:50]


def ingest_case(
    test_id: str,
    suite: str = "unknown",
    log: str = "",
    stack_trace: str = "",
    diff: str = "",
    metadata: dict[str, Any] | None = None,
    max_chars: int = 20_000,
) -> FailureCase:
    """Build a FailureCase from raw artifact strings (secrets redacted, size capped)."""
    if not test_id or not str(test_id).strip():
        raise ValueError("test_id must be a non-empty string")
    log = redact_secrets((log or "")[:max_chars])
    stack_trace = redact_secrets((stack_trace or "")[:max_chars])
    diff = (diff or "")[:max_chars]
    combined = "\n".join(p for p in (stack_trace, log) if p)
    frames, exc_type, exc_msg = parse_stack_trace(combined)
    changed, _ = parse_diff(diff)
    return FailureCase(
        test_id=str(test_id).strip(), suite=suite or "unknown", log=log,
        stack_trace=stack_trace, diff=diff, exception_type=exc_type,
        exception_message=exc_msg, frames=frames, changed_files=changed,
        error_lines=parse_log(combined), metadata=dict(metadata or {}),
    )


def ingest_directory(path: str | Path, test_id: str, suite: str = "unknown") -> FailureCase:
    """Read ``*.log/*.txt``, ``*trace*``/``*stack*`` and ``*.diff/*.patch`` files from a dir."""
    root = Path(path)
    if not root.is_dir():
        raise FileNotFoundError(f"artifact directory not found: {root}")
    log_parts, trace_parts, diff_parts = [], [], []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        name = p.name.lower()
        if name.endswith((".diff", ".patch")):
            diff_parts.append(text)
        elif "trace" in name or "stack" in name:
            trace_parts.append(text)
        elif name.endswith((".log", ".txt")):
            log_parts.append(text)
    return ingest_case(test_id=test_id, suite=suite, log="\n".join(log_parts),
                       stack_trace="\n".join(trace_parts), diff="\n".join(diff_parts))


def ingest_files(log_path=None, stack_path=None, diff_path=None, test_id="unknown", suite="unknown") -> FailureCase:
    def _read(p):
        return Path(p).read_text(encoding="utf-8", errors="replace") if p else ""
    return ingest_case(test_id=test_id, suite=suite, log=_read(log_path),
                       stack_trace=_read(stack_path), diff=_read(diff_path))
