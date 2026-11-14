"""data_setup.py — reproducible fixture environment for a flaky test.

``seed_fixture_env`` writes a small deterministic environment (JSON files +
a manifest with a content hash) that ``run_flaky_checkout`` runs against.
Same seed -> byte-identical files -> the flaky test's outcome is reproducible
in the sense that its *preconditions* are identical every run; the flake
itself is modelled as a seeded coin-flip so a given (seed, attempt) pair
always gives the same result, which is what makes it debuggable in tests.
"""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

DEFAULT_SEED = 42


def _content(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    users = [{"id": i, "name": f"user-{i}", "balance_cents": rng.randrange(0, 10_000)} for i in range(1, 6)]
    orders = [
        {"id": 100 + i, "user_id": rng.choice(users)["id"], "total_cents": rng.randrange(100, 5_000), "status": "pending"}
        for i in range(5)
    ]
    return {
        "seed": seed,
        "users": users,
        "orders": orders,
        "config": {"currency": "USD", "checkout_timeout_s": 5, "feature_flags": {"new_checkout": True}},
    }


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def seed_fixture_env(root: str | Path, seed: int = DEFAULT_SEED, overwrite: bool = True) -> dict[str, Any]:
    root = Path(root)
    if root.exists() and any(root.iterdir()) and not overwrite:
        raise FileExistsError(f"fixture env already exists at {root} (pass overwrite=True)")
    root.mkdir(parents=True, exist_ok=True)
    data = _content(int(seed))
    manifest = {"seed": int(seed), "content_hash": _hash(data), "files": ["db.json", "config.json"]}
    (root / "db.json").write_text(json.dumps({"users": data["users"], "orders": data["orders"]}, indent=2, sort_keys=True), encoding="utf-8")
    (root / "config.json").write_text(json.dumps(data["config"], indent=2, sort_keys=True), encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def load_fixture_env(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    db = json.loads((root / "db.json").read_text(encoding="utf-8"))
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    data = {"seed": manifest["seed"], **db, "config": config}
    # Integrity: re-hash the reconstructed logical content.
    expected = _hash(_content(manifest["seed"]))
    if manifest["content_hash"] != expected:
        raise ValueError("fixture manifest hash does not match seeded content")
    return {**data, "manifest": manifest}


def run_flaky_checkout(root: str | Path, order_id: int = 100, attempt: int = 0) -> dict[str, Any]:
    """Simulated flaky checkout: deterministic for a given (seed, attempt).

    Fails ~1 in 4 attempts with a timeout unless the order/user fixture is
    missing, in which case it always fails with a fixture error — the two
    failure modes triage has to tell apart.
    """
    env = load_fixture_env(root)
    order = next((o for o in env["orders"] if o["id"] == order_id), None)
    if order is None:
        return {"passed": False, "error": "missing_fixture: order not found in seeded db", "order_id": order_id, "attempt": attempt}
    user = next((u for u in env["users"] if u["id"] == order["user_id"]), None)
    if user is None:
        return {"passed": False, "error": "missing_fixture: user not found in seeded db", "order_id": order_id, "attempt": attempt}
    rng = random.Random(f"{env['seed']}:{order_id}:{attempt}")
    if rng.random() < 0.25:
        return {"passed": False, "error": "timeout: checkout timed out after 5s", "order_id": order_id, "attempt": attempt}
    return {"passed": True, "error": "", "order_id": order_id, "attempt": attempt, "charged_cents": order["total_cents"]}
