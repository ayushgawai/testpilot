import json
import pytest
from testpilot.data_setup import load_fixture_env, run_flaky_checkout, seed_fixture_env
from testpilot.evals import evaluate, generate_corpus
from testpilot.report import render_markdown, summarize_runs


def test_data_setup_reproducible(tmp_path):
    m1 = seed_fixture_env(tmp_path / "a", seed=42)
    m2 = seed_fixture_env(tmp_path / "b", seed=42)
    assert m1 == m2
    assert (tmp_path / "a" / "db.json").read_bytes() == (tmp_path / "b" / "db.json").read_bytes()
    m3 = seed_fixture_env(tmp_path / "c", seed=7)
    assert m3["content_hash"] != m1["content_hash"]


def test_data_setup_load_and_flaky_deterministic(tmp_path):
    seed_fixture_env(tmp_path / "env", seed=42)
    env = load_fixture_env(tmp_path / "env")
    assert len(env["users"]) == 5 and len(env["orders"]) == 5
    r1 = run_flaky_checkout(tmp_path / "env", order_id=100, attempt=0)
    r2 = run_flaky_checkout(tmp_path / "env", order_id=100, attempt=0)
    assert r1 == r2
    missing = run_flaky_checkout(tmp_path / "env", order_id=9999)
    assert not missing["passed"] and "missing_fixture" in missing["error"]


def test_data_setup_refuses_overwrite(tmp_path):
    seed_fixture_env(tmp_path / "env")
    with pytest.raises(FileExistsError):
        seed_fixture_env(tmp_path / "env", overwrite=False)


def test_report_pass_rates_and_blockers():
    runs = [
        {"suite": "checkout", "test_id": "test_a", "passed": True},
        {"suite": "checkout", "test_id": "test_b", "passed": False, "root_cause": "timeout"},
        {"suite": "checkout", "test_id": "test_b", "passed": False, "root_cause": "timeout"},
        {"suite": "auth", "test_id": "test_c", "passed": True},
    ]
    s = summarize_runs(runs)
    assert s["suites"]["checkout"]["pass_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert s["suites"]["checkout"]["blockers"][0]["test_id"] == "test_b"
    assert s["overall"]["total"] == 4
    md = render_markdown(s)
    assert "checkout" in md and "Pass rate" in md


def test_evals_measures_top1_and_latency():
    corpus = generate_corpus(n=16, seed=42)
    assert len(corpus) == 16 and all(r.ground_truth for r in corpus)
    result = evaluate(corpus=corpus)
    d = result.to_dict()
    assert 0.0 <= d["top1_accuracy"] <= 1.0
    assert d["median_time_to_first_hypothesis_ms"] >= 0
    # Corpus is built so the mock's signals identify the labelled cause.
    assert d["top1_accuracy"] >= 0.8
