from __future__ import annotations

import math
from typing import Any

from forven.gauntlet.models import normalize_step_key


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def validate_robustness_payload(step_key: object, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_step_key(step_key)
    if not isinstance(payload, dict):
        return {"ok": False, "reason": "robustness payload is not an object"}
    if payload.get("error"):
        return {"ok": False, "reason": str(payload.get("error"))}

    if normalized == "walk_forward":
        splits = _as_list(payload.get("splits"))
        folds = _as_list(payload.get("folds"))
        fold_count = int(_as_float(payload.get("n_folds") or payload.get("fold_count") or len(splits) or len(folds), 0.0))
        if fold_count < 2:
            return {"ok": False, "reason": "walk-forward needs at least 2 folds with out-of-sample evidence"}
        return {"ok": True, "reason": "walk-forward payload has fold evidence"}

    if normalized == "monte_carlo":
        simulations = int(_as_float(payload.get("n_simulations") or payload.get("simulations") or payload.get("num_simulations"), 0.0))
        paths = _as_list(payload.get("equity_paths_sample"))
        if simulations < 100 and not paths:
            return {"ok": False, "reason": "Monte Carlo needs simulation-count or sampled path evidence"}
        min_trades = int(_as_float(payload.get("min_trades") or payload.get("_min_trades"), 10.0))
        trade_count = int(
            _as_float(
                payload.get("n_trades")
                or payload.get("trade_count")
                or payload.get("total_trades"),
                0.0,
            )
        )
        if trade_count < max(1, min_trades):
            return {"ok": False, "reason": f"Monte Carlo needs at least {min_trades} baseline trades"}
        return {"ok": True, "reason": "Monte Carlo payload has simulation evidence"}

    if normalized == "parameter_jitter":
        # A strategy with no numeric params to jitter cannot be parameter-overfit;
        # the analysis records an explicit NOT_APPLICABLE verdict (no reruns, no
        # pass rate). That IS the evidence — demanding iterations here would
        # convert "test does not apply" into a failed gate for every composite.
        if payload.get("not_applicable") is True:
            return {"ok": True, "reason": "parameter jitter not applicable (no numeric parameters)"}
        iterations = int(
            _as_float(
                payload.get("n_iterations")
                or payload.get("iterations")
                or payload.get("samples")
                or payload.get("n_variants")
                or payload.get("variants"),
                0.0,
            )
        )
        has_rate = any(key in payload for key in ("pass_rate", "stable_pct", "pct_positive_sharpe"))
        if iterations < 10 or not has_rate:
            return {"ok": False, "reason": "parameter jitter needs iterations and stability/pass-rate evidence"}
        return {"ok": True, "reason": "parameter jitter payload has stability evidence"}

    if normalized == "cost_stress":
        # Align with what _run_cost_stress actually emits: `original`, `stressed`
        # (each a metrics dict) and `degradation_pct`. The old key list matched on
        # `degradation_pct`, which is ALWAYS present (initialized to 0.0), so the check
        # was vacuous — it verified nothing about whether the stressed rerun ran. Require
        # a real stressed metrics dict with a finite Sharpe.
        stressed = payload.get("stressed")
        if not isinstance(stressed, dict) or not isinstance(payload.get("original"), dict):
            return {"ok": False, "reason": "cost stress missing original/stressed rerun metrics"}
        if "total_trades" not in stressed:
            return {"ok": False, "reason": "cost stress stressed-rerun has no trade evidence"}
        s_sharpe = stressed.get("sharpe_ratio", stressed.get("sharpe"))
        try:
            ok_sharpe = s_sharpe is not None and math.isfinite(float(s_sharpe))
        except (TypeError, ValueError):
            ok_sharpe = False
        if not ok_sharpe:
            return {"ok": False, "reason": "cost stress stressed-rerun has no finite Sharpe"}
        return {"ok": True, "reason": "cost stress payload has original+stressed rerun metrics"}

    if normalized == "regime_split":
        regimes = _as_list(payload.get("regimes"))
        n_regimes = int(_as_float(payload.get("n_regimes") or len(regimes), 0.0))
        if n_regimes < 2:
            return {"ok": False, "reason": "regime split needs at least 2 regimes to be meaningful"}
        return {"ok": True, "reason": "regime split payload has multi-regime evidence"}

    return {"ok": False, "reason": f"unknown robustness step: {step_key}"}
