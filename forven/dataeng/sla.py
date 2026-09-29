"""Freshness SLA: the one definition of how current a stored series must be.

The gauntlet data gate, quality reports, the Data Manager catalog and
coverage views, health alerts and the collection queue all classify freshness
through this module, so the UI, the gate and the alerts cannot disagree.

Lag is measured from the last stored bar's OPEN time (the gauntlet gate's
basis). A perfectly current series therefore has a lag in [tf, 2 x tf): the
newest closed bar opened one to two bar-widths ago. The allowed lag for a
consumer tier is

    allowed = max((missed_bars + 1) x timeframe, floor_minutes)

with ``missed_bars``/``floor_minutes`` per tier from
``data_engine_settings.sla_tiers``. The "pipeline" tier is the gauntlet data
gate's own limit (3 missed bars, 2 h floor).

States:
- ``missing``: no stored data;
- ``frozen``: deliberately not collected (delisted, renamed, struck out);
- ``fresh``: lag <= allowed;
- ``late``: allowed < lag <= breach_multiplier x allowed;
- ``breach``: lag > breach_multiplier x allowed.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import pandas as pd

TIERS: tuple[str, ...] = ("live", "paper", "pipeline", "universe", "idle")
STATES: tuple[str, ...] = ("fresh", "late", "breach", "frozen", "missing")

# Collection priority weight per tier: a live series one allowance late
# outranks a research series twenty allowances late.
TIER_WEIGHTS: dict[str, float] = {
    "live": 100.0,
    "paper": 50.0,
    "pipeline": 20.0,
    "universe": 5.0,
    "idle": 1.0,
}

_DEFAULT_TIERS: dict[str, dict[str, float]] = {
    "live": {"missed_bars": 1, "floor_minutes": 20},
    "paper": {"missed_bars": 2, "floor_minutes": 45},
    "pipeline": {"missed_bars": 3, "floor_minutes": 120},
    "universe": {"missed_bars": 6, "floor_minutes": 360},
    "idle": {"missed_bars": 24, "floor_minutes": 1440},
}
_DEFAULT_BREACH_MULTIPLIER = 3.0

_TIMEFRAME_SECONDS: dict[str, float] = {
    "1m": 60.0,
    "3m": 180.0,
    "5m": 300.0,
    "15m": 900.0,
    "30m": 1800.0,
    "45m": 2700.0,
    "1h": 3600.0,
    "2h": 7200.0,
    "4h": 14400.0,
    "6h": 21600.0,
    "8h": 28800.0,
    "12h": 43200.0,
    "1d": 86400.0,
    "3d": 259200.0,
    "1w": 604800.0,
}


def timeframe_seconds(timeframe: str) -> float:
    """Bar width in seconds for a timeframe string ("15m", "1h", "1d", "1w").

    Raises ValueError for an unrecognised timeframe so a typo cannot silently
    get some default allowance."""
    tf = str(timeframe or "").strip()
    if tf in _TIMEFRAME_SECONDS:
        return _TIMEFRAME_SECONDS[tf]
    number, unit = tf[:-1], tf[-1:]
    # "M" is a month; every other unit is case-insensitive ("1H" == "1h").
    scale = 30 * 86400.0 if unit == "M" else {"m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}.get(unit.lower())
    if number.isdigit() and int(number) > 0 and scale is not None:
        return int(number) * scale
    raise ValueError(f"unrecognised timeframe: {timeframe!r}")


@dataclass(frozen=True)
class SlaPolicy:
    """A frozen snapshot of the SLA settings, so a loop over hundreds of series
    classifies them all against the same thresholds with one settings read."""

    tiers: Mapping[str, Mapping[str, float]]
    breach_multiplier: float

    def tier_rule(self, tier: str) -> Mapping[str, float]:
        return self.tiers.get(tier) or self.tiers.get("idle") or _DEFAULT_TIERS["idle"]

    def allowed_lag_seconds(self, timeframe: str, tier: str) -> float:
        rule = self.tier_rule(tier)
        missed = max(0.0, float(rule.get("missed_bars", 0) or 0))
        floor_s = max(0.0, float(rule.get("floor_minutes", 0) or 0)) * 60.0
        return max((missed + 1.0) * timeframe_seconds(timeframe), floor_s)


def _coerce_tiers(raw: object) -> dict[str, dict[str, float]]:
    tiers = {name: dict(rule) for name, rule in _DEFAULT_TIERS.items()}
    if isinstance(raw, Mapping):
        for name, rule in raw.items():
            if name not in tiers or not isinstance(rule, Mapping):
                continue
            for key in ("missed_bars", "floor_minutes"):
                try:
                    value = float(rule.get(key, tiers[name][key]))
                except (TypeError, ValueError):
                    continue
                if value >= 0:
                    tiers[name][key] = value
    return tiers


def policy_from_settings(settings: Any) -> SlaPolicy:
    """Build a policy from a DataEngineSettings (or a plain dict of its fields)."""
    if isinstance(settings, Mapping):
        raw_tiers = settings.get("sla_tiers")
        raw_mult = settings.get("sla_breach_multiplier")
    else:
        raw_tiers = getattr(settings, "sla_tiers", None)
        raw_mult = getattr(settings, "sla_breach_multiplier", None)
    try:
        mult = float(raw_mult) if raw_mult is not None else _DEFAULT_BREACH_MULTIPLIER
    except (TypeError, ValueError):
        mult = _DEFAULT_BREACH_MULTIPLIER
    return SlaPolicy(tiers=_coerce_tiers(raw_tiers), breach_multiplier=max(1.0, mult))


_POLICY_TTL_SECONDS = 30.0
_policy_lock = threading.Lock()
_policy_cache: tuple[float, SlaPolicy] | None = None


def load_policy(*, refresh: bool = False) -> SlaPolicy:
    """Current SLA policy from Settings -> Data, cached for 30 s. Falls back to
    the defaults when settings cannot be read, never raises."""
    global _policy_cache
    now = time.monotonic()
    with _policy_lock:
        if not refresh and _policy_cache is not None and now - _policy_cache[0] < _POLICY_TTL_SECONDS:
            return _policy_cache[1]
    try:
        from forven.dataeng.settings import load_data_engine_settings

        policy = policy_from_settings(load_data_engine_settings())
    except Exception:
        policy = SlaPolicy(tiers=_coerce_tiers(None), breach_multiplier=_DEFAULT_BREACH_MULTIPLIER)
    with _policy_lock:
        _policy_cache = (now, policy)
    return policy


def clear_policy_cache() -> None:
    global _policy_cache
    with _policy_lock:
        _policy_cache = None


def _to_utc_timestamp(value: object) -> pd.Timestamp | None:
    """Accepts epoch milliseconds, datetime, pandas Timestamp or ISO string."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            ts = pd.Timestamp(int(value), unit="ms", tz="UTC")
        else:
            ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def lag_seconds(last_bar_open: object, *, now: object | None = None) -> float | None:
    """Seconds since the last stored bar's open time; None when unknown."""
    last = _to_utc_timestamp(last_bar_open)
    if last is None:
        return None
    current = _to_utc_timestamp(now) if now is not None else pd.Timestamp(datetime.now(timezone.utc))
    if current is None:
        return None
    return max(0.0, (current - last).total_seconds())


def allowed_lag_seconds(timeframe: str, tier: str, *, policy: SlaPolicy | None = None) -> float:
    return (policy or load_policy()).allowed_lag_seconds(timeframe, tier)


def classify(
    lag: float | None,
    timeframe: str,
    tier: str,
    *,
    frozen: bool = False,
    policy: SlaPolicy | None = None,
) -> str:
    """SLA state for a series with the given lag (seconds)."""
    if frozen:
        return "frozen"
    if lag is None:
        return "missing"
    active = policy or load_policy()
    allowed = active.allowed_lag_seconds(timeframe, tier)
    if lag <= allowed:
        return "fresh"
    if lag <= allowed * active.breach_multiplier:
        return "late"
    return "breach"


# Lateness counts at most this many allowances toward priority, so a research
# series that has been dead for months can never outrank late live data
# (10 x universe weight 5 = 50 < 1.2 x paper weight 50).
PRIORITY_RATIO_CAP = 10.0


def priority(lag: float | None, timeframe: str, tier: str, *, policy: SlaPolicy | None = None) -> float:
    """Collection priority: allowances late (capped at PRIORITY_RATIO_CAP),
    weighted by tier. A missing series ranks as the cap."""
    active = policy or load_policy()
    allowed = active.allowed_lag_seconds(timeframe, tier)
    ratio = PRIORITY_RATIO_CAP if lag is None else (lag / allowed if allowed > 0 else 0.0)
    return min(ratio, PRIORITY_RATIO_CAP) * TIER_WEIGHTS.get(tier, 1.0)


def assess(
    last_bar_open: object,
    timeframe: str,
    tier: str,
    *,
    frozen: bool = False,
    now: object | None = None,
    policy: SlaPolicy | None = None,
) -> dict[str, Any]:
    """Full SLA assessment of one series, in the wire shape the Data Manager
    API returns (``SlaAssessment`` in the contract)."""
    active = policy or load_policy()
    lag = lag_seconds(last_bar_open, now=now)
    allowed = active.allowed_lag_seconds(timeframe, tier)
    last = _to_utc_timestamp(last_bar_open)
    return {
        "tier": tier,
        "state": classify(lag, timeframe, tier, frozen=frozen, policy=active),
        "lag_seconds": None if lag is None else round(lag, 1),
        "allowed_seconds": round(allowed, 1),
        "ratio": None if lag is None or allowed <= 0 else round(lag / allowed, 3),
        "last_bar_ts": last.isoformat().replace("+00:00", "Z") if last is not None else None,
        "priority": round(priority(lag, timeframe, tier, policy=active), 3) if not frozen else 0.0,
    }


def gate_allowed_staleness_hours(timeframe: str, *, policy: SlaPolicy | None = None) -> float:
    """The gauntlet data gate's freshness limit (the "pipeline" tier) in hours."""
    return allowed_lag_seconds(timeframe, "pipeline", policy=policy) / 3600.0


__all__ = [
    "STATES",
    "TIERS",
    "TIER_WEIGHTS",
    "PRIORITY_RATIO_CAP",
    "SlaPolicy",
    "allowed_lag_seconds",
    "assess",
    "classify",
    "clear_policy_cache",
    "gate_allowed_staleness_hours",
    "lag_seconds",
    "load_policy",
    "policy_from_settings",
    "priority",
    "timeframe_seconds",
]
