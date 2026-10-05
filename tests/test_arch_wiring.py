"""Cross-file WIRING contracts.

Every check here guards a producer/consumer pair that a file-disjoint refactor
can silently break: one side ships a new field, guard or verdict, and the other
side never learns about it. A unit test on either file alone stays green while
the contract between them is dead. These tests fail when the two ends drift.

Slow subprocess-backed checks are marked and skip cleanly when the environment
cannot supply them; the rest are pure and fast.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "frontend" / "src" / "lib" / "settings" / "manifest.ts"


# ---------------------------------------------------------------------------
# ARCH-05: the settings manifest must not lie about the engine's defaults
# ---------------------------------------------------------------------------
# frontend/src/lib/settings/manifest.ts carries a `default` per knob. It is BOTH
# the "Default: N" caption and the value the field renders when the backend has
# no stored value — so a stale number is the Settings page telling the operator a
# gate is stricter (or looser) than the engine actually runs. That page is what an
# operator reviews before arming live.
#
# Nine gate thresholds had drifted from DEFAULT_PIPELINE_CONFIG (e.g. "Quick
# screen min trades: 30" against an engine running 20) and one entry carried a
# literal `// TODO confirm default`. The real fix is to stop duplicating the
# numbers and read them from the backend; until the settings shell can do that,
# this test is the thing that makes duplication safe.

# Ratio rails: policy stores a fraction (0.33) and the UI field is %-shaped (33).
# Either form is accepted by the backend normalizer, so both are valid here.
_RATIO_TOLERANT_SUFFIX = ("_pct", "_rate_min", "_percentile_min", "mc_max_dd_p95")

# The ONE knowingly-divergent entry. frontend/src/tests/settingsManifest.test.ts
# (M-15 suite) pins this manifest default at 1.05 — the value the gate enforced
# when the knob was first wired. The backend's Default preset has since relaxed
# quick_screen.min_profit_factor to 1.0 ("keep only a not-a-clear-loser screen at
# the cheapest triage"; the Strict preset restores 1.1), so the vitest expectation
# is the stale end, not the manifest. Fixing it means editing that test file and
# the manifest in ONE change and deleting this entry — it is exempted, not
# forgiven, so the divergence stays counted.
_KNOWN_MANIFEST_DEFAULT_DRIFT: set[str] = set()


def _parse_manifest_entries() -> list[dict]:
    """Extract {id, default, backendSection, backendPath} from manifest.ts.

    A regex reader rather than a TS parse: the manifest is a flat array of object
    literals with one field per line, and pulling in a JS runtime to read four
    fields would make this test unrunnable in the Python suite.
    """
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    entries: list[dict] = []
    for match in re.finditer(r"\n  \{\n(.*?)\n  \},", text, re.S):
        body = match.group(1)

        def field(name: str, _body: str = body) -> str | None:
            found = re.search(rf"^    {name}: (.+?),?$", _body, re.M)
            return found.group(1).rstrip(",") if found else None

        entry_id = field("id")
        if not entry_id:
            continue
        entries.append(
            {
                "id": entry_id.strip("'\""),
                "default_raw": field("default"),
                "backend_section": (field("backendSection") or "").strip("'\""),
                "backend_path": (field("backendPath") or "").strip("'\""),
                "line": text[: match.start()].count("\n") + 2,
            }
        )
    return entries


def _decode_default(raw: str | None):
    if raw is None:
        return None, False
    try:
        return json.loads(raw.replace("'", '"')), True
    except Exception:
        return raw, False


def _resolve(root: dict, dotted: str):
    node = root
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def _values_agree(manifest_value, backend_value, dotted: str) -> bool:
    if manifest_value == backend_value:
        return True
    numeric = (
        isinstance(manifest_value, (int, float))
        and not isinstance(manifest_value, bool)
        and isinstance(backend_value, (int, float))
        and not isinstance(backend_value, bool)
    )
    if not numeric:
        return False
    if abs(float(manifest_value) - float(backend_value)) < 1e-9:
        return True
    # %-shaped UI field over a fraction-shaped rail.
    if dotted.endswith(_RATIO_TOLERANT_SUFFIX):
        return abs(float(manifest_value) - float(backend_value) * 100.0) < 1e-9
    return False


def test_manifest_parses_and_is_not_empty():
    entries = _parse_manifest_entries()
    assert len(entries) > 150, f"manifest reader found only {len(entries)} entries — regex drifted"
    assert all(e["default_raw"] is not None for e in entries if e["backend_section"] == "pipeline")


@pytest.mark.dsr_gate_default
def test_manifest_defaults_match_the_engine_defaults():
    """Every manifest default that resolves in the backend must equal it."""
    from forven.api_core import _DEFAULT_SETTINGS_PAYLOAD
    from forven.policy import DEFAULT_PIPELINE_CONFIG

    drift: list[str] = []
    exempt_seen: set[str] = set()
    for entry in _parse_manifest_entries():
        path = entry["backend_path"]
        if not path:
            continue
        if entry["backend_section"] == "pipeline" and "." in path:
            backend_value, found = _resolve(DEFAULT_PIPELINE_CONFIG, path)
        else:
            backend_value, found = _resolve(_DEFAULT_SETTINGS_PAYLOAD, path)
        if not found:
            # Knobs whose defaults live in another module's own defaults dict
            # (maintenance retention, data-engine settings, …). Out of scope for
            # this check — it asserts agreement where BOTH ends are resolvable.
            continue
        manifest_value, decoded = _decode_default(entry["default_raw"])
        if not decoded:
            continue
        if _values_agree(manifest_value, backend_value, path):
            assert path not in _KNOWN_MANIFEST_DEFAULT_DRIFT, (
                f"{path} now agrees with the backend — delete it from "
                "_KNOWN_MANIFEST_DEFAULT_DRIFT (and check frontend/src/tests/"
                "settingsManifest.test.ts pins the same value)"
            )
            continue
        if path in _KNOWN_MANIFEST_DEFAULT_DRIFT:
            exempt_seen.add(path)
            continue
        drift.append(
            f"manifest.ts:{entry['line']} {entry['id']} -> {path}: "
            f"manifest={manifest_value!r} backend={backend_value!r}"
        )

    assert not drift, (
        "Settings manifest defaults disagree with the engine's defaults. The page "
        "would show the operator a gate the engine does not run:\n  " + "\n  ".join(drift)
    )
    assert exempt_seen == _KNOWN_MANIFEST_DEFAULT_DRIFT, (
        "an exempted manifest drift no longer exists in the manifest — prune "
        f"_KNOWN_MANIFEST_DEFAULT_DRIFT (missing: {_KNOWN_MANIFEST_DEFAULT_DRIFT - exempt_seen})"
    )


def test_manifest_carries_no_unconfirmed_default_markers():
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    assert "TODO confirm default" not in text, (
        "a manifest default is marked unconfirmed; confirm it against the backend "
        "instead of shipping an unverified number to the pre-live review page"
    )


def test_manifest_surfaces_the_wfa_min_folds_safety_floor():
    """safety_floors.wfa_min_folds must be editable, like every other rail.

    wfa-min-folds-has-no-safety-floor: it was the only gauntlet threshold with no
    floor, so gauntlet.wfa_min_folds=1 let one lucky OOS window carry a paper
    promotion. The floor exists now; an unsurfaced floor is a dead knob.
    """
    from forven.policy import DEFAULT_PIPELINE_CONFIG

    entry = next(
        (e for e in _parse_manifest_entries() if e["backend_path"] == "safety_floors.wfa_min_folds"),
        None,
    )
    assert entry is not None, "safety_floors.wfa_min_folds is missing from the settings manifest"
    assert entry["backend_section"] == "pipeline"
    manifest_value, decoded = _decode_default(entry["default_raw"])
    assert decoded and manifest_value == DEFAULT_PIPELINE_CONFIG["safety_floors"]["wfa_min_folds"]


# ---------------------------------------------------------------------------
# Lookahead probe: producer (probe_lookahead) -> consumers
# ---------------------------------------------------------------------------
# probe_lookahead reports `inconclusive` when the truncation comparison compared
# NOTHING (an all-quiet strategy used to be stamped leak-free on zero evidence).
# It is never a rejection. Without consumers recording it the fix is inert.


def test_worker_validation_payload_carries_probe_verifiability():
    """The sandbox worker must emit the verifiability fields the parent reads.

    intake.lookahead_verifiability() consumes exactly these keys off the worker's
    metadata blob; a worker that omits them reads as "verifiable" by default, so
    every registration was stamped verified regardless of what the probe saw.
    """
    source = (REPO_ROOT / "forven" / "sandbox" / "strategy_worker.py").read_text(encoding="utf-8")
    assert "probe_lookahead(probe)" in source, (
        "the registration worker must use the structured probe, not the "
        "reason-only detect_lookahead wrapper"
    )
    for key in (
        "lookahead_verifiable",
        "lookahead_inconclusive_reason",
        "bounded_lookback",
    ):
        assert f'"{key}"' in source, f"worker validation payload is missing {key}"


def test_isolated_validation_reports_verifiability_end_to_end(monkeypatch):
    """Spawn the real worker and check the keys survive the process boundary.

    Slow (one subprocess), but it is the only check that proves the contract
    across the process boundary rather than by reading source.
    """
    monkeypatch.delenv("FORVEN_IN_STRATEGY_WORKER", raising=False)
    from forven.sandbox.strategy_worker import StrategyWorkerError, validate_custom_module_isolated
    from forven.strategies import registry
    from forven.strategies.intake import lookahead_verifiability

    registry.discover()
    for type_name, cls in sorted(registry._TYPE_MAP.items()):
        module = str(getattr(cls, "__module__", ""))
        if ".custom." not in module or not isinstance(type_name, str):
            continue
        try:
            meta = validate_custom_module_isolated(module.split(".")[-1])
        except StrategyWorkerError:
            continue
        if not meta.get("ok"):
            continue
        assert "lookahead_verifiable" in meta, "worker dropped the verifiability fields"
        normalized = lookahead_verifiability(meta)
        # A blocked strategy is a separate axis; verifiability must be a bool either way.
        assert isinstance(normalized["lookahead_verifiable"], bool)
        if not normalized["lookahead_verifiable"]:
            assert normalized["lookahead_inconclusive_reason"]
        return
    pytest.skip("no custom strategy module available for isolated validation")


def test_reentry_verdict_reports_inconclusive_without_blocking(monkeypatch):
    """Research recovery must record "not verified" without turning it into a reject."""
    import forven.brain as brain
    import forven.strategies.lookahead_probe as lookahead_probe

    class _Quiet:
        def __init__(self, *_a, **_k):
            pass

    monkeypatch.setattr(
        "forven.strategies.backtest._resolve_strategy_class", lambda _t: _Quiet, raising=True
    )
    monkeypatch.setattr(lookahead_probe, "detect_lookahead", lambda _obj: None)
    monkeypatch.setattr(
        lookahead_probe,
        "probe_lookahead",
        lambda _obj: lookahead_probe.LookaheadVerdict(inconclusive="compared nothing"),
    )

    reason, inconclusive = brain._reentry_lookahead_verdict("S1", "any_type", {})
    assert reason is None, "an inconclusive probe must never become a rejection"
    assert inconclusive == "compared nothing"
    # Back-compat surface for gauntlet.engine, which imports the reason-only helper.
    assert brain._reentry_lookahead_reason("S1", "any_type", {}) is None


def test_reentry_verdict_still_blocks_on_a_leak(monkeypatch):
    import forven.brain as brain
    import forven.strategies.lookahead_probe as lookahead_probe

    class _Leaky:
        def __init__(self, *_a, **_k):
            pass

    monkeypatch.setattr(
        "forven.strategies.backtest._resolve_strategy_class", lambda _t: _Leaky, raising=True
    )
    monkeypatch.setattr(lookahead_probe, "detect_lookahead", lambda _obj: "Lookahead detected: leak")

    reason, inconclusive = brain._reentry_lookahead_verdict("S1", "any_type", {})
    assert reason == "Lookahead detected: leak"
    assert inconclusive is None
    assert brain._reentry_lookahead_reason("S1", "any_type", {}) == "Lookahead detected: leak"


def test_reentry_verdict_fails_open_but_marks_unverified(monkeypatch):
    """A probe-INFRASTRUCTURE fault never blocks recovery — and never claims proof."""
    import forven.brain as brain

    def _boom(_t):
        raise RuntimeError("probe module unavailable")

    monkeypatch.setattr(
        "forven.strategies.backtest._resolve_strategy_class", _boom, raising=True
    )
    reason, inconclusive = brain._reentry_lookahead_verdict("S1", "any_type", {})
    assert reason is None
    assert inconclusive and "could not run" in inconclusive


# ---------------------------------------------------------------------------
# HL-CLOSE-1 caller side: a close that did not fill is not a close
# ---------------------------------------------------------------------------


def test_basket_close_outcome_rejects_a_confirmed_short_fill():
    """A partial reduce-only fill must not be reported as a completed close."""
    from forven.basket_live import _close_outcome

    partial = _close_outcome({"exit_price": 100.0, "filled_size": 4.0}, 10.0)
    assert partial["ok"] is False
    assert partial["close_outcome"] == "partial"
    assert partial["residual_units"] == pytest.approx(6.0)
    assert "residual" in str(partial["error"])

    full = _close_outcome({"exit_price": 100.0, "filled_size": 10.0}, 10.0)
    assert full["ok"] is True and full["close_outcome"] == "filled"

    rejected = _close_outcome({"error": "close order returned no fill (rejected by exchange)"}, 10.0)
    assert rejected["ok"] is False and rejected["error"]

    # An ambiguous response (no filled_size at all) stays `ok` — the reconcile
    # re-reads real venue positions, so it self-corrects — but is stamped so the
    # ambiguity reaches the ledger instead of being flattened into a green tick.
    unknown = _close_outcome({"exit_price": 100.0}, 10.0)
    assert unknown["ok"] is True and unknown["close_outcome"] == "unknown"


def test_force_close_rejection_leaves_the_trade_open(forven_db, monkeypatch):
    """close_position's HL-CLOSE-1 error must never be booked as a close."""
    from types import SimpleNamespace

    import forven.exchange.hyperliquid as hl
    from forven.api_domains.trading import force_close_trade
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, direction, entry_price, signal_entry_price,
             fill_entry_price, size, risk_pct, leverage, status, execution_type, source,
             signal_data, opened_at)
            VALUES ('t-reject', 'S-REJECT', 'S-REJECT', 'BTC', 'long', 50000, 50000,
                    50000, 0.1, 0.01, 1, 'OPEN', 'live', 'manual', '{}', datetime('now'))
            """
        )

    monkeypatch.setattr(
        hl,
        "close_position",
        lambda *a, **k: {
            "mid": 50000.0,
            "close_price": 48500.0,  # the 3%-through-mid limit that never traded
            "exit_price": None,
            "filled_size": None,
            "error": "close order returned no fill (rejected by exchange)",
        },
    )

    result = force_close_trade("t-reject", SimpleNamespace(reason="test"))
    assert result["ok"] is False
    assert result.get("close_rejected") is True
    assert result.get("position_still_open") is True

    with get_db() as conn:
        row = conn.execute("SELECT status, exit_price FROM trades WHERE id = 't-reject'").fetchone()
    assert row["status"] == "OPEN", "a close that did not fill must not close the trade"
    assert row["exit_price"] is None, "the never-traded limit price must not be booked as an exit"


# ---------------------------------------------------------------------------
# Unarmed mainnet exit -> operator notification
# ---------------------------------------------------------------------------


def test_unarmed_mainnet_exit_log_raises_a_notification(forven_db):
    """The guard's CRITICAL record must reach the operator, not just a log file."""
    from forven import notifications
    from forven.exchange import hyperliquid as hl_mod

    notifications._alert_bridge_installed = False
    # Take the connector's ACTUAL logger object rather than naming it here. The
    # first cut of this bridge listened on "forven.exchange.hyperliquid" while the
    # connector logs to "forven.exchange.hl" — siblings, not parent/child, so the
    # handler never saw a record and the alert could never fire. A test that
    # hardcodes the name reproduces that bug instead of catching it.
    exchange_log = hl_mod.log
    assert exchange_log.name == notifications._EXCHANGE_ALERT_LOGGER, (
        f"alert bridge listens on {notifications._EXCHANGE_ALERT_LOGGER!r} but the "
        f"connector logs to {exchange_log.name!r} — the bridge is dead"
    )
    before = len(exchange_log.handlers)
    try:
        assert notifications.install_exchange_alert_bridge() is True
        assert notifications.install_exchange_alert_bridge() is False, "must be idempotent"

        exchange_log.critical(
            "UNARMED MAINNET EXIT PERMITTED: a reduce-only close resolved to the MAINNET "
            "endpoint while FORVEN_ALLOW_MAINNET is NOT set."
        )
        stored = notifications.list_notifications(limit=10)
        assert any(n.get("event_type") == "mainnet_unarmed_exit" for n in stored), (
            "the unarmed-mainnet-exit alert did not become a notification"
        )
        alert = next(n for n in stored if n.get("event_type") == "mainnet_unarmed_exit")
        assert alert.get("severity") == "critical"

        # An unrelated critical on the same logger must not raise a money alert.
        count_before = len(notifications.list_notifications(limit=50))
        exchange_log.critical("get_all_mids: serving 900s-old cached mids to an EXIT path")
        assert len(notifications.list_notifications(limit=50)) == count_before
    finally:
        for handler in list(exchange_log.handlers[before:]):
            exchange_log.removeHandler(handler)
        notifications._alert_bridge_installed = False


# ---------------------------------------------------------------------------
# Daemon: never republish breaker-open cached mids with a fresh timestamp
# ---------------------------------------------------------------------------


def test_daemon_treats_an_open_price_breaker_as_unreachable(monkeypatch):
    """STALE-REPUBLISH-1: the fallback poll must not restamp its own cache.

    With the breaker open, get_all_mids returns the daemon's OWN cached mids;
    publishing them refreshes `market:prices.updated_at` AND `last_tick_ts`, which
    is the very clock hyperliquid._cached_mids_age_seconds measures staleness by.
    """
    import time as _time

    import forven.daemon as daemon
    import forven.exchange.hyperliquid as hl
    from forven.circuit_breaker import State

    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    breaker = hl.hl_price_breaker
    orig = (breaker.state, breaker.failure_count, breaker.last_failure_time, breaker.half_open_calls)
    try:
        # Drive the REAL breaker rather than stubbing a method name — stubbing
        # `can_execute` is how the first cut hid the bug below.
        breaker.state = State.OPEN
        breaker.last_failure_time = _time.time()
        assert daemon._hl_price_api_is_reachable() is False

        breaker.state = State.CLOSED
        assert daemon._hl_price_api_is_reachable() is True

        # The probe must NOT consume the recovery budget. `can_execute()` is a
        # CONSUME operation: in HALF_OPEN it spends one of half_open_max_calls
        # (2 here), so probing with it twice would starve the real get_all_mids
        # call, the breaker could never close, and the fallback poll would stay
        # dark forever.
        breaker.state = State.HALF_OPEN
        breaker.half_open_calls = 0
        for _ in range(5):
            assert daemon._hl_price_api_is_reachable() is True
        assert breaker.half_open_calls == 0, (
            "the reachability probe consumed HALF_OPEN budget — it must be read-only"
        )
    finally:
        breaker.state, breaker.failure_count, breaker.last_failure_time, breaker.half_open_calls = orig


def test_daemon_price_api_probe_fails_open_on_an_unknown_breaker(monkeypatch):
    """An unreadable breaker must behave exactly as before, not go dark."""
    import builtins

    import forven.daemon as daemon

    real_import = builtins.__import__

    def _deny(name, *args, **kwargs):
        if name == "forven.exchange.hyperliquid":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _deny)
    assert daemon._hl_price_api_is_reachable() is True


# ---------------------------------------------------------------------------
# Propr mirror -> /api/propr/mirror
# ---------------------------------------------------------------------------


def test_propr_mirror_payload_surfaces_unmanaged_and_halt_anchor(forven_db, monkeypatch):
    """Orphaned venue positions and the daily-anchor provenance must reach the page."""
    import forven.propr_mirror as propr_mirror
    from forven.routers import propr as propr_router

    monkeypatch.setattr(propr_router, "_require_enabled", lambda: None)
    monkeypatch.setattr(propr_mirror, "mirror_enabled", lambda *a, **k: True)
    monkeypatch.setattr(propr_mirror, "mirror_roster", lambda *a, **k: {})
    monkeypatch.setattr(propr_mirror, "roster_candidates", lambda *a, **k: [])
    monkeypatch.setattr(propr_mirror, "get_state", lambda: {})
    monkeypatch.setattr(
        propr_mirror,
        "get_halt_state",
        lambda: {
            "anchor_source": "first_observation",
            "daily_rule_fully_enforced": False,
            "halted": False,
        },
    )
    monkeypatch.setattr(
        propr_mirror, "get_unmanaged_state", lambda: {"positions": [{"asset": "BTC", "units": 0.5}]}
    )

    payload = propr_router.propr_get_mirror()
    assert payload["unmanaged"] == {"positions": [{"asset": "BTC", "units": 0.5}]}
    assert payload["halt"]["daily_rule_fully_enforced"] is False
    assert payload["halt"]["anchor_source"] == "first_observation"


def test_propr_mirror_halt_state_stamps_the_anchor_fields():
    """The producer side: _evaluate_halt must publish what the panel reads."""
    from forven.propr_mirror import _evaluate_halt

    assert "anchor_source" in _evaluate_halt.__doc__
    import inspect

    source = inspect.getsource(_evaluate_halt)
    assert '"anchor_source"' in source
    assert '"daily_rule_fully_enforced"' in source


# ---------------------------------------------------------------------------
# Agent instructions must not point at a retired capability
# ---------------------------------------------------------------------------


def test_full_stack_engineer_is_not_told_to_use_run_code():
    """AI-01: run_code is an AST-guarded numeric scratchpad, not an inspection tool."""
    source = (REPO_ROOT / "forven" / "bot.py").read_text(encoding="utf-8")
    assert "run_code for diagnosis" not in source, (
        "the full-stack-engineer instruction still points at run_code; its AST guard "
        "rejects os/sqlite3/pathlib/requests/forven.db, so the agent burns a round on "
        "a guaranteed rejection"
    )
