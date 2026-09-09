"""Backtest-engine provenance: version every stats-affecting artifact.

The problem this solves: after an engine fix (exit ordering, fee model, sizing,
indicator math, ...) every previously persisted backtest / validation verdict
describes a DIFFERENT engine than the one running today. Comparing those stale
numbers against fresh ones silently corrupted gate decisions — strategies were
archived on verdicts the current engine would never produce, and every engine
fix required a MANUAL re-baseline sweep plus manual triage of wrongly-killed
strategies.

The contract:

* every persisted backtest artifact (backtest_results.config_json), gauntlet
  settings snapshot / artifact payload, strategy verdict blob and gate-rejection
  record is stamped with ``BACKTEST_ENGINE_VERSION`` at write time;
* a STATS-AFFECTING engine change bumps ``BACKTEST_ENGINE_VERSION`` (with a
  required ``ENGINE_VERSION_LOG`` entry — a unit test enforces the changelog);
* readers treat a stamped artifact from a DIFFERENT version as STALE: it is
  never compared against fresh numbers (policy._extract_gauntlet_verdict_payloads
  refuses it), the promotion gate blocks with the counter-exempt
  ``stale_engine_artifacts`` reason code, and the gauntlet sweep
  (engine.requeue_stale_engine_artifacts) automatically re-queues the strategy
  for re-validation — including reviving strategies the old engine archived;
* artifacts WITHOUT a stamp (written before this module shipped) are
  grandfathered as current — the operator's last manual re-baseline is the
  baseline for pre-provenance history. Staleness only ever fires on an explicit
  version mismatch, mirroring the params_hash convention in gauntlet/status.py.

Bump discipline: bump for changes that alter backtest STATISTICS (fills, fees,
slippage, sizing, exit ordering, indicator math, metric formulas). Do NOT bump
for pure refactors, logging, UI, or performance work — a bump re-queues every
active strategy's validation suite, which is exactly the point, and exactly the
cost.

This module must stay import-light (constants + dict helpers only): it is
imported from api_core, policy and the gauntlet engine, so any heavy import
here would create cycles.
"""

from __future__ import annotations

import json

# The current stats-affecting backtest-engine version. Bump on any change that
# alters what the engine would compute for the SAME strategy + data — or on a
# data-substrate rebuild that invalidates comparisons against prior verdicts
# (verdicts are only evidence relative to the data they were scored on) — and
# add a matching ENGINE_VERSION_LOG entry (test-enforced).
BACKTEST_ENGINE_VERSION = 10

# Append-only changelog: one entry per version, newest last. The unit test
# asserts the newest entry matches BACKTEST_ENGINE_VERSION so a bump can never
# ship without recording why.
ENGINE_VERSION_LOG: tuple[dict, ...] = (
    {
        "version": 1,
        "date": "2026-07-02",
        "summary": (
            "Provenance baseline. History predating this stamp is grandfathered "
            "as current (operator re-baselined manually after the round-3 engine "
            "fixes); staleness detection starts from here."
        ),
    },
    {
        "version": 2,
        "date": "2026-07-06",
        "summary": (
            "Combined data re-baseline (edge-data expansion): spot-mix perp "
            "rebuild (2026-07-02), ~50-symbol deep-history research-universe "
            "seed, DVOL backfill to 2021-03, and coverage-aware liquidation "
            "fill (pre-capture bars NaN, not fake zeros). Verdicts scored on "
            "the pre-rebuild data are stale evidence against the current lake."
        ),
    },
    {
        "version": 3,
        "date": "2026-07-08",
        "summary": (
            "Regime-gate adx_min fix: adx_min was only enforced when an adx_cap "
            "was also present, so trend strategies setting adx_min alone ran "
            "unfiltered (user report: Donchian +adx_min:25 backtested identical "
            "to no filter). It now gates entries and forces exits on its own. "
            "Any strategy stamping adx_min without adx_max scores differently."
        ),
    },
    {
        "version": 4,
        "date": "2026-07-10",
        "summary": (
            "Backtest correctness re-baseline: entry-bar protective exits and "
            "liquidation, shared hedged exposure, exit-notional fee/slippage, "
            "mark-to-market portfolio return/drawdown, chronological purged WFA, "
            "disjoint optimizer holdout, and point-in-time appended-bar filtering."
        ),
    },
    {
        "version": 5,
        "date": "2026-07-11",
        "summary": (
            "Metric-semantics re-baseline: bar-level CALENDAR Sharpe/Sortino from the "
            "mark-to-market equity curve are now the primary sharpe/sortino (trade-based "
            "EVENT values preserved as trade_sharpe/trade_sortino) — sparse-trading "
            "strategies read a LOWER, cadence-comparable Sharpe; funding-aware Kelly "
            "evidence (perp funding accrued inside the kernel walk so kelly sizing learns "
            "funding-adjusted returns, applied exactly once); unified regime ATR-ratio "
            "baseline (signal-walk aligned to the 30-bar baseline robustness + live use, "
            "was 44-bar); and true fixed-DOLLAR notional (fixed-mode sizes off equity at "
            "entry, so deployed notional stays ~fixed_size instead of scaling with equity)."
        ),
    },
    {
        "version": 6,
        "date": "2026-07-25",
        "summary": (
            "Funding-cost + lake-substrate re-baseline (HARDEN-DATA-OPS). (1) Perp "
            "funding is now charged over each print's OWN settlement interval instead "
            "of a hardcoded /8: 4h-settlement pairs were mis-charged 2x, and the "
            "whole-file divisor inflated funding ~8x on 1h bars — every carry-sensitive "
            "backtest's net PnL, Sharpe and Kelly evidence changes. (2) OHLCV "
            "forward-fill now runs per fetched BLOCK and the fabricated (no-trade) bars "
            "are marked, so trade-reconstructed candles no longer smear a flat synthetic "
            "price across gaps between windows — bar counts, volatility and drawdown on "
            "gappy symbols differ from the pre-fix lake. Verdicts scored on the old "
            "funding model or the unmarked forward fill are stale evidence and are "
            "re-queued rather than compared against current numbers."
        ),
    },
    {
        "version": 7,
        "date": "2026-09-06",
        "summary": (
            "Causal execution phases: allocate all opening entries before intrabar "
            "protection, preventing retrospective reentry and future equity sizing. "
            "Enrichment now preserves missing coverage and expires stale observations; "
            "hourly aggregates are delayed even for one-row histories."
        ),
    },
    {
        "version": 8,
        "date": "2026-09-06",
        "summary": (
            "Reserve built-in strategy type names during custom discovery. A generated "
            "module can no longer replace a built-in implementation and silently change "
            "signals between inline and isolated execution. Earlier stamped results "
            "must be revalidated against the unambiguous runtime."
        ),
    },
)

ENGINE_VERSION_LOG += ({
    "version": 9,
    "date": "2026-09-08",
    "summary": "Reject custom runtime replacement and fuzzy family substitution; reject signed scalar Signal entries and record source identity. Prior results require validation against the exact implementation.",
},)

ENGINE_VERSION_KEY = "engine_version"

ENGINE_VERSION_LOG += ({
    "version": 10,
    "date": "2026-09-08",
    "summary": "Size overlapping entries from reserved dollar margin and entry-equity snapshots; retain full-precision realized sizing capital and share pro-rata allocation with forward execution.",
},)


def stamp_engine_version(config: dict | None) -> dict:
    """Return ``config`` (copied if None) with the current engine version stamped.

    An existing stamp is preserved — completion writers merge over the
    submission-time config and must not re-stamp a run that STARTED on an older
    engine as if it ran on the current one.
    """
    stamped = dict(config) if isinstance(config, dict) else {}
    stamped.setdefault(ENGINE_VERSION_KEY, BACKTEST_ENGINE_VERSION)
    return stamped


def artifact_engine_version(config: object) -> int | None:
    """Extract the stamped engine version from a config dict or JSON text.

    Returns None when the artifact predates provenance stamping (or the blob is
    unreadable) — callers must treat None as "unknown, grandfathered current",
    never as stale.
    """
    blob = config
    if isinstance(blob, (str, bytes, bytearray)):
        try:
            blob = json.loads(blob)
        except Exception:
            return None
    if not isinstance(blob, dict):
        return None
    raw = blob.get(ENGINE_VERSION_KEY)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def is_stale_engine_artifact(config: object) -> bool:
    """True only when the artifact carries an EXPLICIT stamp from another version.

    Unstamped (pre-provenance) artifacts are never stale — see module docstring.
    """
    stamped = artifact_engine_version(config)
    return stamped is not None and stamped != BACKTEST_ENGINE_VERSION


__all__ = [
    "BACKTEST_ENGINE_VERSION",
    "ENGINE_VERSION_KEY",
    "ENGINE_VERSION_LOG",
    "artifact_engine_version",
    "is_stale_engine_artifact",
    "stamp_engine_version",
]
