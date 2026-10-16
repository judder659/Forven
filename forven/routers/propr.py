"""Propr.xyz prop-firm integration API (PROPR-1).

Deliberately hidden: every route except GET /api/propr/status returns 404
while the hidden integration flag (forven.config.propr_enabled) is off, and
status itself reports only {"enabled": false} in that state — a casual caller
learns nothing. The flag is env/config-only (FORVEN_PROPR_ENABLED) and is NOT
in the settings manifest; OPENING a position additionally requires
FORVEN_ALLOW_PROPR_LIVE, while closes, protective legs and cancels need only
the integration flag (PROPR-PERM-2 — see forven/exchange/propr.py).
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from forven.api_security import require_operator_access
from forven.config import propr_enabled

router = APIRouter(tags=["propr"], dependencies=[Depends(require_operator_access)])


def _require_enabled() -> None:
    if not propr_enabled():
        # 404 (not 403): while the flag is off this surface does not exist.
        raise HTTPException(status_code=404, detail="Not Found")


def _propr():
    from forven.exchange import propr
    return propr


class ApiKeyRequest(BaseModel):
    api_key: str


class MirrorConfigRequest(BaseModel):
    enabled: bool | None = None
    strategies: list[str] | None = None


class ClosePositionRequest(BaseModel):
    asset: str
    position_side: str = Field(pattern="^(long|short)$")
    quantity: float = Field(gt=0)
    confirm: bool = False


@router.get("/api/propr/status")
def propr_status(remote: bool = True):
    return _propr().get_status(include_remote=remote)


@router.put("/api/propr/api-key")
def propr_set_api_key(body: ApiKeyRequest):
    _require_enabled()
    key = str(body.api_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="api_key is required")
    from forven.db import kv_get, kv_set
    from forven.secret_storage import encrypt_secret

    secrets_blob = kv_get("forven:settings:secrets", {}) or {}
    if not isinstance(secrets_blob, dict):
        secrets_blob = {}
    secrets_blob["propr_api_key"] = encrypt_secret(key)
    kv_set("forven:settings:secrets", secrets_blob)
    return {"ok": True, "status": _propr().get_status()}


@router.delete("/api/propr/api-key")
def propr_clear_api_key():
    _require_enabled()
    from forven.db import kv_get, kv_set

    secrets_blob = kv_get("forven:settings:secrets", {}) or {}
    if isinstance(secrets_blob, dict) and "propr_api_key" in secrets_blob:
        secrets_blob.pop("propr_api_key", None)
        kv_set("forven:settings:secrets", secrets_blob)
    return {"ok": True}


@router.get("/api/propr/overview")
def propr_overview():
    """Everything the Propr page renders in one call. Remote reads are
    independent best-efforts — one failing section must not blank the page."""
    _require_enabled()
    propr = _propr()
    overview: dict = {"status": propr.get_status()}
    for key, fn in (
        ("positions", propr.raw_positions),
        ("orders", lambda: propr.list_orders(limit=50)),
        ("trades", lambda: propr.list_trades(limit=50)),
        ("attempts", propr.list_challenge_attempts),
        ("challenges", propr.list_challenges),
    ):
        try:
            overview[key] = fn()
        except Exception as exc:
            overview[key] = None
            overview.setdefault("errors", {})[key] = str(exc)
    return overview


@router.get("/api/propr/mirror")
def propr_get_mirror():
    """Mirror config + per-trade mirror state + strategies the picker offers.

    The mirror is an observer: it copies roster strategies' trades onto the
    Propr account and never touches live/paper execution itself.

    ``unmanaged`` (PROPR-RECONCILE-1) carries the last reconcile's ORPHANED venue
    positions — real leveraged exposure on the challenge account that no mirror
    state entry accounts for (a lost KV write, a hand-placed order). The mirror
    already detects and records them; without surfacing them here the page showed
    a tidy roster while the account carried positions nothing was watching.

    ``halt`` carries ``anchor_source`` / ``daily_rule_fully_enforced`` (PROPR-ANCHOR-1)
    so the challenge-rules panel can say "daily rule only PARTIALLY enforced today"
    on a day whose opening balance we never observed, instead of drawing a gauge
    that implies the venue's daily-loss rule is fully mirrored.
    """
    _require_enabled()
    from forven import propr_mirror

    halt = propr_mirror.get_halt_state()
    # SLICE-1: the challenge account is divided equally across the roster, so the
    # panel can answer "how big will each member trade?" instead of leaving it to
    # be inferred. Derived from the halt state's equity (the same figure the daily
    # rules are measured against) so the two can never disagree on screen.
    capital_slice = None
    try:
        equity = float((halt or {}).get("equity") or 0.0)
        if equity > 0:
            _slice, meta = propr_mirror.mirror_equity_slice(equity)
            # MIRROR-RISK-1: the RESOLVED risk setting, so the page shows the
            # number the next order will actually use (not a hardcoded caption).
            risk_frac = propr_mirror.mirror_risk_fraction()
            meta["risk_pct"] = round(risk_frac * 100.0, 4)
            if meta.get("slice_usd"):
                meta["risk_usd_per_trade"] = round(float(meta["slice_usd"]) * risk_frac, 2)
            capital_slice = meta
    except Exception:  # noqa: BLE001 — a display figure must never break the panel
        capital_slice = None

    return {
        "enabled": propr_mirror.mirror_enabled(),
        "strategies": propr_mirror.mirror_roster(),
        "candidates": propr_mirror.roster_candidates(),
        "state": propr_mirror.get_state(),
        "halt": halt,
        "unmanaged": propr_mirror.get_unmanaged_state(),
        "capital_slice": capital_slice,
    }


@router.get("/api/propr/scorecard")
def propr_scorecard(refresh: bool = False):
    """Per-strategy record of the mirror: venue-realized PnL and R for every
    mirrored trade, each strategy's own forward record since it joined, the
    trades the mirror missed and why, and roster health (PROPR-SCORE-1)."""
    _require_enabled()
    from forven.propr_scorecard import get_scorecard

    return get_scorecard(force=refresh)


@router.put("/api/propr/mirror")
def propr_update_mirror(body: MirrorConfigRequest):
    _require_enabled()
    if body.enabled is None and body.strategies is None:
        raise HTTPException(status_code=400, detail="nothing to update")
    if body.enabled and not _propr().get_api_key():
        raise HTTPException(status_code=400, detail="configure the Propr API key first")
    from forven import propr_mirror

    result = propr_mirror.set_mirror_config(
        enabled=body.enabled, strategy_ids=body.strategies
    )
    return {"ok": True, **result}


@router.post("/api/propr/mirror/tick")
def propr_mirror_tick_now():
    """Force one observer pass (the scheduler runs it every 60s anyway)."""
    _require_enabled()
    from forven.propr_mirror import mirror_tick

    return {"ok": True, "result": mirror_tick()}


@router.post("/api/propr/positions/close")
def propr_close_position(body: ClosePositionRequest):
    """Manual reduce-only close of a Propr position from the page.

    NOT gated by FORVEN_ALLOW_PROPR_LIVE. A close is risk-REDUCING, so it goes
    through the adapter's reduce lane (PROPR-PERM-2), which asks only for the
    integration flag — refusing an exit would strand the position this endpoint
    exists to unwind. Opens are the ones that need the opt-in.
    """
    _require_enabled()
    if not body.confirm:
        raise HTTPException(status_code=400, detail="confirm=true is required")
    side = "sell" if body.position_side == "long" else "buy"
    try:
        result = _propr().close_position(body.asset, body.quantity, side)
    except RuntimeError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=502, detail=str(result["error"]))
    return {"ok": True, "result": result}


@router.post("/api/propr/orders/{order_id}/cancel")
def propr_cancel_order(order_id: str):
    _require_enabled()
    try:
        result = _propr().cancel_order("", order_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=502, detail=str(result["error"]))
    return {"ok": True, "result": result}


@router.post("/api/propr/connection-test")
def propr_connection_test():
    """Read-only reachability probe: health, auth, attempts, positions."""
    _require_enabled()
    propr = _propr()
    report: dict = {"base_url": propr.get_base_url()}
    checks: list[dict] = []

    def _check(name: str, fn):
        try:
            value = fn()
            checks.append({"name": name, "ok": True, "detail": value})
        except Exception as exc:
            checks.append({"name": name, "ok": False, "detail": str(exc)})

    _check("user", lambda: propr.get_user())
    _check("attempts", lambda: propr.list_challenge_attempts())
    _check("account", lambda: propr.resolve_account(force_refresh=True))
    _check("positions", lambda: propr.raw_positions())
    report["checks"] = checks
    report["ok"] = all(c["ok"] for c in checks)
    return report
