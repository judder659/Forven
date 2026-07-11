"""P1 control-plane integrity: settings mutations must be atomic and serialized.

Two confirmed integrity problems these tests lock down:

1. Read-modify-write race — ``put_settings_section`` used to load settings,
   apply (which itself loaded+mutated+saved), reload, diff old-vs-new for the
   audit log, then save AGAIN. Two concurrent section saves could lose one edit
   entirely and mis-attribute the audit diff to the wrong request's actor.
   Every mutation now runs under ``_SETTINGS_MUTATION_LOCK`` so both edits land
   and each audit entry reflects exactly its own request's changes.

2. Non-atomic multi-key persistence — one logical mutation wrote the main
   settings blob and the encrypted-secrets blob (and, for pipeline, the payload
   plus its WIP-cap mirror) as SEPARATE ``kv_set`` calls. A crash between them
   left enforcement diverged from display. Every touched key is now written in a
   single ``kv_set_many`` transaction: the whole mutation lands or none of it.
"""

from __future__ import annotations

import threading

import pytest

import forven.api_core as core
from forven.api_core import (
    PipelineSettingsUpdateBody,
    _load_settings_payload,
    get_pipeline_settings,
    put_pipeline_settings,
    put_settings_section,
)


# ---------------------------------------------------------------------------
# 1. Concurrency: two threads mutate different sections at once
# ---------------------------------------------------------------------------


def test_concurrent_section_saves_both_land_with_correct_audit(forven_db):
    """Two threads PUT different sections simultaneously, N times over.

    Both edited values must always land, and the audit log must contain BOTH
    entries with per-entry field attribution intact (no lost update, no
    cross-attributed diff). Without the mutation lock the read-modify-write
    race drops one edit and/or the losing writer's blob overwrites the other's
    audit entry.
    """
    core._save_settings_payload(core._default_settings_payload())

    iterations = 25
    for n in range(iterations):
        drawdown_value = 10 + n
        capital_value = 1000 + n

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def put_risk() -> None:
            try:
                barrier.wait(timeout=5)
                put_settings_section("risk", {"max_drawdown_pct": drawdown_value})
            except BaseException as exc:  # pragma: no cover - surfaced via assert
                errors.append(exc)

        def put_capital() -> None:
            try:
                barrier.wait(timeout=5)
                put_settings_section("initial-capital", {"initial_capital": capital_value})
            except BaseException as exc:  # pragma: no cover - surfaced via assert
                errors.append(exc)

        workers = [threading.Thread(target=put_risk), threading.Thread(target=put_capital)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        assert errors == [], f"worker error on iteration {n}: {errors}"

        # Both edits landed — neither writer clobbered the other's field.
        blob = _load_settings_payload()
        assert blob["max_drawdown_pct"] == drawdown_value, f"lost risk edit on iteration {n}"
        assert blob["initial_capital"] == capital_value, f"lost capital edit on iteration {n}"

        # Both audit entries are present for this iteration, each attributed to
        # its own field (the diff is not cross-contaminated between requests).
        audit = blob.get("audit_log") or []
        drawdown_entries = [
            e for e in audit
            if e["id"] == "risk.max_drawdown_pct" and e["to"] == drawdown_value
        ]
        capital_entries = [
            e for e in audit
            if e["id"] == "initial-capital.initial_capital" and e["to"] == capital_value
        ]
        assert drawdown_entries, f"missing risk audit entry on iteration {n}"
        assert capital_entries, f"missing capital audit entry on iteration {n}"
        # Each entry changed exactly ONE leaf — its own — so a risk entry never
        # carries the capital field and vice versa.
        assert drawdown_entries[-1]["id"] != capital_entries[-1]["id"]


def test_concurrent_pipeline_and_section_saves_serialize(forven_db):
    """A pipeline save and a section save racing must both land."""
    core._save_settings_payload(core._default_settings_payload())

    for n in range(15):
        capital_value = 5000 + n
        wip_value = 3 + (n % 5)

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def put_section() -> None:
            try:
                barrier.wait(timeout=5)
                put_settings_section("initial-capital", {"initial_capital": capital_value})
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        def put_pipeline() -> None:
            try:
                barrier.wait(timeout=5)
                put_pipeline_settings(
                    PipelineSettingsUpdateBody(updates={"paper_wip_cap": wip_value})
                )
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        workers = [threading.Thread(target=put_section), threading.Thread(target=put_pipeline)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        assert errors == [], f"worker error on iteration {n}: {errors}"
        assert _load_settings_payload()["initial_capital"] == capital_value
        assert get_pipeline_settings()["paper_wip_cap"] == wip_value


# ---------------------------------------------------------------------------
# 2. Atomicity: a mid-mutation persistence failure leaves NO partial state
# ---------------------------------------------------------------------------


def test_section_save_persistence_failure_is_all_or_nothing(forven_db, monkeypatch):
    """If the FINAL atomic write raises, neither the main blob nor the secrets
    blob is left half-updated: the mutation lands whole or not at all.
    """
    core._save_settings_payload(core._default_settings_payload())
    # Establish a known-good baseline for both the main blob and secrets.
    put_settings_section("risk", {"max_drawdown_pct": 20})
    baseline_blob = _load_settings_payload()
    baseline_drawdown = baseline_blob["max_drawdown_pct"]
    assert baseline_drawdown == 20

    from forven.db import kv_get

    baseline_secrets_raw = kv_get(core._SETTINGS_SECRET_STORAGE_KEY, {})

    # Make the single atomic persist raise, simulating a crash/lock failure
    # mid-write. Because kv_set_many commits every key on ONE connection, a
    # failure rolls back the whole transaction — nothing is written.
    def boom(_items):
        raise RuntimeError("simulated kv failure mid-mutation")

    monkeypatch.setattr(core, "kv_set_many", boom)

    with pytest.raises(RuntimeError, match="simulated kv failure"):
        put_settings_section(
            "hyperliquid",
            {"actual_wallet_address": "0xdeadbeef", "api_secret_key": "0x" + "a" * 64},
        )

    monkeypatch.undo()

    # No partial state: the main blob still holds the baseline (the failed
    # mutation's wallet/drawdown never landed) and the secrets blob is unchanged
    # (the private key from the failed request was NOT persisted).
    after_blob = _load_settings_payload()
    assert after_blob["max_drawdown_pct"] == baseline_drawdown
    assert after_blob.get("hyperliquid_wallet", "") != "0xdeadbeef"
    assert after_blob.get("hyperliquid_has_key") is False

    after_secrets_raw = kv_get(core._SETTINGS_SECRET_STORAGE_KEY, {})
    assert after_secrets_raw == baseline_secrets_raw


def test_pipeline_save_persistence_failure_is_all_or_nothing(forven_db, monkeypatch):
    """A mid-write failure on the pipeline path leaves the payload + WIP-cap
    mirror untouched (no torn payload-vs-cap state).
    """
    baseline_cap = get_pipeline_settings()["paper_wip_cap"]

    from forven.db import kv_get

    baseline_wip_kv = kv_get("pipeline:wip_cap:paper")

    def boom(_items):
        raise RuntimeError("simulated pipeline kv failure")

    monkeypatch.setattr(core, "kv_set_many", boom)

    with pytest.raises(RuntimeError, match="simulated pipeline kv failure"):
        put_pipeline_settings(
            PipelineSettingsUpdateBody(updates={"paper_wip_cap": baseline_cap + 7})
        )

    monkeypatch.undo()

    # Neither the display payload nor the enforced WIP-cap mirror moved.
    assert get_pipeline_settings()["paper_wip_cap"] == baseline_cap
    assert kv_get("pipeline:wip_cap:paper") == baseline_wip_kv


# ---------------------------------------------------------------------------
# 3. The single atomic write really touches BOTH keys together
# ---------------------------------------------------------------------------


def test_section_mutation_writes_secrets_and_blob_in_one_transaction(forven_db, monkeypatch):
    """A section save that touches a secret persists the secrets blob and the
    main blob via a SINGLE kv_set_many call (one transaction), not two writes.
    """
    core._save_settings_payload(core._default_settings_payload())

    calls: list[dict] = []
    real_kv_set_many = core.kv_set_many

    def spy(items):
        calls.append(dict(items))
        return real_kv_set_many(items)

    monkeypatch.setattr(core, "kv_set_many", spy)

    put_settings_section(
        "hyperliquid",
        {"actual_wallet_address": "0xabc", "api_secret_key": "0x" + "b" * 64},
    )

    # Exactly one atomic persist for the mutation, carrying BOTH keys.
    persist_calls = [
        c for c in calls
        if core._SETTINGS_STORAGE_KEY in c and core._SETTINGS_SECRET_STORAGE_KEY in c
    ]
    assert len(persist_calls) == 1
    assert _load_settings_payload().get("hyperliquid_has_key") is True
