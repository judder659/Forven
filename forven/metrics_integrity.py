"""Data-quality invariants for backtest metrics.

These checks detect metric payloads that are *implausible* rather than merely
bad — the signature of an engine or data bug, not a weak strategy. The
canonical example is the 2026-06 dropna regression: sparse funding/OI columns
silently evicted the entire in-sample window, so ~680 strategies were
auto-rejected on "0 in-sample trades" while their out-of-sample legs traded
actively. Nothing in the pipeline noticed for 13 days because the gates
consumed the zeros as legitimate failures.

Anomalous metrics must be quarantined for investigation — flagged, alerted,
and held out of gate evaluation — never treated as a normal rejection or
allowed to terminally retire a strategy.
"""

from __future__ import annotations

# Key under which anomaly descriptions are stored inside a persisted metrics
# payload so downstream consumers (UI, gates, sweeps) can see the quarantine.
DATA_QUALITY_FLAGS_KEY = "data_quality_flags"

# Gate-reason prefix for data-quality holds. Deliberately does NOT contain
# "(reject)" — the pipeline hygiene sweep treats "(reject)" gate text as a
# terminal failure and archives the strategy, which is exactly the wrong
# response to a suspected engine/data bug.
DATA_QUALITY_HOLD_PREFIX = "DataQualityHold"

# An out-of-sample leg with at least this many trades is considered "active":
# a zero-trade in-sample leg alongside it is implausible because the in-sample
# window is the larger share of the same dataset with the same parameters.
_MIN_ACTIVE_OOS_TRADES = 10

# The reverse direction needs a higher bar: a quiet out-of-sample window can
# legitimately happen in a low-signal regime, so only flag when the in-sample
# leg was clearly active.
_MIN_ACTIVE_IS_TRADES = 30


def _to_int(value: object) -> int | None:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return parsed


def _to_float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


# Implausible-metric thresholds — the fingerprint of a look-ahead / data leak
# (a uniform future-bar leak makes IS and OOS BOTH "perfect"), NOT a strong
# strategy. A real tradable edge does not peg the Sharpe clamp or sustain these.
# Detection lives here (not only in the policy gate) because the policy gate is
# short-circuited under ``testing_mode`` while this runs on the persist path and
# both brain transition guardrails, which are never bypassed.
_RISK_RATIO_CLAMP = 10.0           # == backtest._MAX_ABS_RISK_RATIO; a Sharpe AT the clamp = leaked
_MAX_PLAUSIBLE_SHARPE = 6.0        # |Sharpe| at/above this over a full window is a leak signature
_MAX_PLAUSIBLE_PROFIT_FACTOR = 10.0
_MIN_TRADES_FOR_PF_CHECK = 15      # a high PF on a tiny sample is noise, not a leak
_MAX_PLAUSIBLE_RETURN = 100.0      # |total_return_pct| (fraction) of 100 = 100x; honest backtests are far below


def _implausible_leg(leg: object, label: str) -> list[str]:
    """Flag implausible Sharpe / profit-factor / return on one IS|OOS leg."""
    if not isinstance(leg, dict):
        return []
    out: list[str] = []
    sharpe = _to_float(leg.get("sharpe"))
    if sharpe is not None:
        if abs(sharpe) >= _RISK_RATIO_CLAMP - 0.01:
            out.append(
                f"{label} Sharpe {sharpe:.2f} is pegged at the +/-{_RISK_RATIO_CLAMP:.0f} clamp "
                "-- near-certain look-ahead / data leak, not a real edge"
            )
        elif abs(sharpe) >= _MAX_PLAUSIBLE_SHARPE:
            out.append(
                f"{label} Sharpe {sharpe:.2f} is implausibly high (|Sharpe| >= {_MAX_PLAUSIBLE_SHARPE:g}) "
                "-- likely a data leak"
            )
    pf = _to_float(leg.get("profit_factor"))
    trades = _to_int(leg.get("total_trades")) or 0
    if pf is not None and pf >= _MAX_PLAUSIBLE_PROFIT_FACTOR and trades >= _MIN_TRADES_FOR_PF_CHECK:
        out.append(
            f"{label} profit_factor {pf:.2f} >= {_MAX_PLAUSIBLE_PROFIT_FACTOR:g} over {trades} trades is implausible"
        )
    ret = _to_float(leg.get("total_return_pct"))
    if ret is not None and abs(ret) >= _MAX_PLAUSIBLE_RETURN:
        out.append(
            f"{label} total_return_pct {ret:.0f} is implausibly large -- likely a data leak"
        )
    return out


def check_metrics_integrity(metrics: object) -> list[str]:
    """Return anomaly descriptions for an IS/OOS metrics payload.

    An empty list means the payload is plausible (which is not the same as
    good). Payloads without the nested in_sample/out_of_sample structure are
    not checkable and pass through unflagged.
    """
    if not isinstance(metrics, dict):
        return []

    is_block = metrics.get("in_sample")
    oos_block = metrics.get("out_of_sample")
    if not isinstance(is_block, dict) or not isinstance(oos_block, dict):
        return []

    anomalies: list[str] = []

    # Implausible Sharpe / profit-factor / return on either leg -- the look-ahead
    # or data-leak fingerprint. Checked regardless of trade counts so a
    # clamped-Sharpe leak is always quarantined (the policy gate's plausibility
    # check is skipped under testing_mode; this path is not).
    anomalies.extend(_implausible_leg(is_block, "in_sample"))
    anomalies.extend(_implausible_leg(oos_block, "out_of_sample"))

    is_trades = _to_int(is_block.get("total_trades"))
    oos_trades = _to_int(oos_block.get("total_trades"))
    # A whole-window run (the gauntlet's holdout confirmation) scores every bar
    # out-of-sample, so its in-sample leg is empty on purpose, not lost.
    whole_window = metrics.get("scored_window") == "whole"
    if is_trades is not None and oos_trades is not None and not whole_window:
        if is_trades == 0 and oos_trades >= _MIN_ACTIVE_OOS_TRADES:
            anomalies.append(
                f"in_sample reports 0 trades while out_of_sample has {oos_trades} — "
                "the in-sample leg was likely lost (data eviction or split bug), "
                "not a quiet strategy"
            )
        if oos_trades == 0 and is_trades >= _MIN_ACTIVE_IS_TRADES:
            anomalies.append(
                f"out_of_sample reports 0 trades while in_sample has {is_trades} — "
                "the out-of-sample leg likely failed to run or its data window is empty"
            )

    return anomalies


def data_quality_hold_reason(anomalies: list[str]) -> str:
    """Build the gate-reason string for a data-quality hold."""
    return f"{DATA_QUALITY_HOLD_PREFIX}: {'; '.join(anomalies)} (held for investigation — not a strategy failure)"
