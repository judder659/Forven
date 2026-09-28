"""Data-substrate provenance: fingerprint the SEMANTICS of the streams a
verdict was scored on, and treat a semantic change as staleness.

The incident this generalizes (2026-07-07): the basket-universe keepalive
rewrote funding history files mid-day, transiently flipping the measured
print cadence for some symbols from 8h to 1h. Every consumer that derives
per-hour rates from the print interval silently accrued ~8x funding on the
affected symbols — a +54%/yr "validated" result recomputed hours later on the
settled files was -14%/yr. Engine provenance (engine_provenance.py) already
protects verdicts against CODE changes; nothing protected them against DATA
semantics changing underneath.

The contract (mirrors engine provenance):

* every persisted backtest artifact is stamped at write time with a compact
  fingerprint of the SEMANTICS of each enrichment stream for its
  symbol/timeframe — print cadence and value-scale bucket, plus an "absent"
  marker for streams with no file. Appends do not move the fingerprint;
  cadence flips, unit rescales, and stream appearance/disappearance do;
* readers treat an artifact stamped with a DIFFERENT fingerprint as STALE
  evidence: policy._extract_gauntlet_verdict_payloads refuses its payload,
  the promotion gate blocks with a counter-exempt reason, and the standard
  missing-artifact flow re-runs the validation on the current data;
* artifacts WITHOUT a stamp (written before this module shipped) are
  grandfathered as current — staleness only fires on an explicit mismatch.

v1 scope note: ACTIVE strategies re-validate through the normal
missing-artifact path. Reviving ARCHIVED strategies after a lake-wide
semantic change remains the engine-version mechanism's job — a change that
big warrants a BACKTEST_ENGINE_VERSION bump (see the v2 log entry, which was
exactly such a data re-baseline).

Fingerprints are TTL-cached: computing one reads the tail of each stream
file, and the staleness check sits on gate-evaluation paths.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from typing import Any

log = logging.getLogger("forven.data_provenance")

DATA_FINGERPRINT_KEY = "data_fingerprint"
DATA_FINGERPRINT_DETAIL_KEY = "data_fingerprint_detail"
# The dataset identity dict (checksum/rows/span/market/as_of) that api_core
# stamps; it once lived under DATA_FINGERPRINT_KEY and shadowed the hash.
DATA_IDENTITY_KEY = "data_identity"

_TAIL_ROWS = 1500
_CACHE_TTL_SECONDS = 600.0
_cache: dict[tuple[str, str], tuple[float, str, dict]] = {}


def _quantize_cadence_hours(hours: float) -> float:
    """Quantize a measured print cadence so jitter never moves the fingerprint.

    >= 45 min rounds to whole hours (1h, 8h, 24h grids); sub-hourly rounds to
    the nearest quarter hour.
    """
    if hours >= 0.75:
        return float(round(hours))
    return round(hours * 4.0) / 4.0


def _scale_bucket(values) -> str:
    """Order-of-magnitude bucket of the p99 |value| — stable under appends,
    flips on unit changes (a per-8h -> per-hour rescale moves ~log10(8))."""
    import numpy as np

    arr = values.dropna().abs()
    arr = arr[arr > 0]
    if arr.empty:
        return "empty"
    p99 = float(np.percentile(arr, 99))
    if p99 <= 0 or not math.isfinite(p99):
        return "empty"
    return f"e{int(math.floor(math.log10(p99)))}"


def stream_semantics(symbol: str, timeframe: str) -> dict[str, Any]:
    """Per-stream semantic descriptors for a symbol/timeframe context.

    Resolves stream files exactly as the production enrichment join does
    (hub._available_enrichment_specs), so the fingerprint describes the data
    the engine actually reads.
    """
    import pandas as pd

    from forven.dataeng.hub import _available_enrichment_specs

    out: dict[str, Any] = {}
    specs = _available_enrichment_specs(
        str(symbol or ""), str(timeframe or "1h"), include_macro=False, exclude_streams=set()
    )
    for spec in specs:
        # One iv spec exists per column (iv_btc / iv_eth) under the same
        # stream name — key by output column so both are described.
        key = spec.output_columns[0] if spec.output_columns else spec.stream
        try:
            if not spec.path.exists():
                out[key] = "absent"
                continue
            frame = pd.read_parquet(spec.path)
            if frame.empty or "timestamp" not in frame.columns:
                out[key] = "absent"
                continue
            tail = frame.tail(_TAIL_ROWS)
            ts = pd.to_datetime(tail["timestamp"], utc=True, errors="coerce").dropna()
            gaps = ts.sort_values().diff().dropna()
            if gaps.empty:
                out[key] = "absent"
                continue
            cadence = _quantize_cadence_hours(float(gaps.median().total_seconds()) / 3600.0)
            value_col = next((c for c in spec.source_columns if c in tail.columns), None)
            scale = _scale_bucket(pd.to_numeric(tail[value_col], errors="coerce")) if value_col else "empty"
            out[key] = {"cadence_h": cadence, "scale": scale}
        except Exception:
            # A transiently unreadable stream must not distinguish itself from
            # absence: both mean "no usable semantics right now".
            log.debug("stream semantics unreadable for %s %s %s", symbol, timeframe, key, exc_info=True)
            out[key] = "absent"
    return out


def data_fingerprint(symbol: str, timeframe: str) -> tuple[str, dict[str, Any]]:
    """(stable short hash, semantics detail) for a symbol/timeframe, TTL-cached."""
    cache_key = (str(symbol or "").strip().upper(), str(timeframe or "1h").strip().lower())
    now = time.monotonic()
    hit = _cache.get(cache_key)
    if hit is not None and (now - hit[0]) < _CACHE_TTL_SECONDS:
        return hit[1], hit[2]
    semantics = stream_semantics(cache_key[0], cache_key[1])
    digest = hashlib.sha1(
        json.dumps(semantics, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    _cache[cache_key] = (now, digest, semantics)
    return digest, semantics


def clear_fingerprint_cache() -> None:
    _cache.clear()


def stamp_data_fingerprint(config: dict | None, symbol: str, timeframe: str) -> dict:
    """Return ``config`` with the current data fingerprint stamped.

    An existing stamp is preserved (completion writers merge over the
    submission-time config). A non-string value under the key is the legacy
    dataset-identity dict, not a stamp: it moves to ``data_identity`` and the
    hash is stamped. Failure-proof: stamping must never break artifact
    persistence, so any fault returns the config unstamped.
    """
    stamped = dict(config) if isinstance(config, dict) else {}
    existing = stamped.get(DATA_FINGERPRINT_KEY)
    if isinstance(existing, str) and existing:
        return stamped
    if existing is not None and not isinstance(existing, str):
        stamped.setdefault(DATA_IDENTITY_KEY, stamped.pop(DATA_FINGERPRINT_KEY))
    try:
        digest, semantics = data_fingerprint(symbol, timeframe)
        stamped[DATA_FINGERPRINT_KEY] = digest
        stamped[DATA_FINGERPRINT_DETAIL_KEY] = semantics
    except Exception:
        log.debug("data fingerprint stamping failed for %s %s", symbol, timeframe, exc_info=True)
    return stamped


def artifact_data_fingerprint(config: object) -> str | None:
    """Extract the stamped fingerprint; None = pre-provenance (grandfathered).
    Only a string is a stamp: the legacy dataset-identity dict that api_core
    wrote under the same key reads as unstamped, never as a mismatch."""
    blob = config
    if isinstance(blob, (str, bytes, bytearray)):
        try:
            blob = json.loads(blob)
        except Exception:
            return None
    if not isinstance(blob, dict):
        return None
    raw = blob.get(DATA_FINGERPRINT_KEY)
    return raw if isinstance(raw, str) and raw else None


def is_stale_data_artifact(config: object, symbol: str, timeframe: str) -> bool:
    """True only when the artifact carries an EXPLICIT stamp that mismatches
    the current data semantics for its symbol/timeframe. Unstamped artifacts
    are never stale; a fingerprint-computation fault is never staleness."""
    stamped = artifact_data_fingerprint(config)
    if stamped is None:
        return False
    try:
        current, _ = data_fingerprint(symbol, timeframe)
    except Exception:
        return False
    return stamped != current


__all__ = [
    "DATA_FINGERPRINT_KEY",
    "DATA_FINGERPRINT_DETAIL_KEY",
    "DATA_IDENTITY_KEY",
    "artifact_data_fingerprint",
    "clear_fingerprint_cache",
    "data_fingerprint",
    "is_stale_data_artifact",
    "stamp_data_fingerprint",
    "stream_semantics",
]
