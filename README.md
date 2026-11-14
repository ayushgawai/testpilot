# testpilot

Agentic triage tool that ranks test-failure root causes and drafts bug reports, exposed over MCP-style tools.

Give it a failing test's log, stack trace and recent diff; it returns ranked
root-cause hypotheses (with confidence and evidence) plus a drafted bug report
(title, repro, suspected cause, suggested fix) — deterministically, with no
network or API key required for the bundled mock provider.

## Design

```
                 +-------------------+
  logs           |                   |
  stack traces   |     ingest.py     |   FailureCase
  recent diff -->|  parse + redact   +--------+
                 |  secrets, cap size|        |
                 +-------------------+        v
                                      +-------------------+
                                      |     triage.py     |
                      LLMProvider     |  TriageAgent      |   TriageResult
                      (Protocol)  --> |  hypotheses rank  +--------+
                      MockProvider    |  + bug report     |        |
                      (deterministic) |  guardrails       |        v
                                      +-------------------+   bug report /
                                             ^                ranked causes
                                             | call_tool(name, args)
                                      +-------------------+
                                      |  MCP-style TOOLS  |
                                      |  triage_failure   |
                                      |  rank_hypotheses  |
                                      |  draft_bug_report |
                                      +-------------------+

  data_setup.py          evals.py / bench.py          report.py
  seed fixture env  -->  historical corpus      -->   pass rates +
  for flaky tests        top-1 / median TTFH          blockers per suite
```

| Module | Responsibility | Key entry points |
|---|---|---|
| `testpilot/ingest.py` | Read logs, stack traces and a unified diff into a structured case; redact secrets, cap size | `ingest_case`, `ingest_directory`, `ingest_files`, `FailureCase` |
| `testpilot/triage.py` | Agentic triage behind a provider-agnostic LLM interface; ranked hypotheses + drafted bug report; guardrails; MCP-style tool definitions | `TriageAgent`, `LLMProvider`, `MockProvider`, `TOOLS`, `call_tool` |
| `testpilot/data_setup.py` | Seed a small, reproducible fixture environment for a flaky checkout test | `seed_fixture_env`, `load_fixture_env`, `run_flaky_checkout` |
| `testpilot/report.py` | Summarise pass rates and blockers per test suite | `summarize_runs`, `render_markdown` |
| `testpilot/evals.py` | Evaluation harness over historical failing runs: top-1 root-cause accuracy, median time-to-first-hypothesis | `generate_corpus`, `evaluate` |
| `testpilot/bench.py` | CLI bench over 500 historical failing runs (scalable with `--runs`) | `python -m testpilot.bench` |
| `tests/` | pytest: triage schema, guardrails, mock determinism, data-setup reproducibility, report + evals | `python -m pytest` |

Root-cause categories: `null_reference`, `timeout`, `assertion_mismatch`,
`missing_fixture`, `race_condition`, `config_error`, `dependency_failure`,
`permission_error`.

The LLM is behind `LLMProvider`, a `typing.Protocol` with one method,
`complete(system, user) -> str` returning a fixed JSON schema. `MockProvider`
implements it with deterministic signal scoring over the case text — same
input, byte-identical output — so tests, evals and the bench are fully
reproducible. A real provider only needs to satisfy that protocol;
`validate_result` enforces the guardrails (known categories, confidences in
[0, 1] sorted descending, at most 5 hypotheses, complete bug report) on any
provider's output.

## Run

```bash
pip install -e ".[dev]"

# Triage one failure via the MCP-style tool surface (python -c or your client):
python -c "from testpilot.triage import call_tool; import json; print(json.dumps(call_tool('triage_failure', {'test_id': 'test_pay', 'log': open('failure.log').read()}), indent=2))"

# Or in Python:
#   from testpilot.ingest import ingest_directory
#   from testpilot.triage import TriageAgent
#   result = TriageAgent().triage(ingest_directory('artifacts/', test_id='test_pay'))
```

External clients invoke the tools by name with the JSON schemas in
`testpilot.triage.TOOLS` (`get_tool_definitions()` / `call_tool(name, args)`) —
the same shape an MCP client sends, without an MCP SDK dependency.

## Test

```bash
python -m pytest -q
```

```
...............                                                          [100%]
15 passed
```

(Count line abbreviated; 15 tests covering triage output schema, guardrails,
determinism of the mock provider, and data-setup reproducibility.)

## Bench

Full bench (500 historical failing runs):

```bash
python -m testpilot.bench --runs 500          # add --json for machine-readable output
```

Sanity run actually executed for this README — **scaled down honestly to
50 runs** (`python -m testpilot.bench --runs 50`):

```
testpilot bench - provider=mock runs=50 seed=42
  root-cause top-1 rate : 100.0%  (50/50)
  median time-to-first-hypothesis: 0.039 ms
  per-category top-1:
    assertion_mismatch     4/4  (100.0%)
    config_error           7/7  (100.0%)
    dependency_failure     7/7  (100.0%)
    missing_fixture        7/7  (100.0%)
    null_reference         9/9  (100.0%)
    permission_error       4/4  (100.0%)
    race_condition         5/5  (100.0%)
    timeout                7/7  (100.0%)
```

The 500-run bench was also run and likewise reports 100.0% (500/500),
median 0.034 ms. **Read that number honestly:** the historical corpus is
synthetic and generated from the same signal vocabulary the mock provider
scores, so 100% top-1 is a pipeline sanity check, not evidence of real-world
accuracy. On real failure logs the ranking is only as good as the provider
behind `LLMProvider`.

## Limitations

- **Mock provider only.** No real LLM provider is wired up yet; `MockProvider`
  is a deterministic keyword/signal scorer. Plugging in a real model means
  implementing `complete(system, user)` — the schema and guardrails already
  constrain its output, but its ranking quality is unmeasured.
- **Synthetic eval corpus.** `evals.generate_corpus` fabricates historical
  runs from templates (see the bench note above). There is no corpus of real,
  labelled production failures, so top-1 accuracy does not transfer as-is.
- **MCP-style, not an MCP server.** Tools are exposed as MCP-shaped
  `{name, description, inputSchema}` definitions plus a `call_tool`
  dispatcher; there is no MCP transport/server process yet.
- **Heuristic ingest.** Stack-trace parsing targets Python-style traces and
  unified diffs; other languages/formats degrade to keyword signals over the
  raw log. Secret redaction is a regex over common key names, not a scanner.
- **Simulated flake.** `data_setup.run_flaky_checkout` models a flaky test
  as a seeded coin-flip against the fixture env; it reproduces preconditions
  deterministically but is not a real checkout system.
- **Latency is in-process.** Median time-to-first-hypothesis measures local
  mock inference only; a network LLM would dominate that number.

Unfinished parts, stated plainly: a real LLM provider, a real MCP transport,
and a real labelled failure corpus are not implemented — the interfaces and
harness for them are.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Ayush Gawai.
