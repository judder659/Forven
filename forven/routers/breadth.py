"""Breadth test: a strategy's frozen rule run across many coins (forven.breadth)."""

from fastapi import APIRouter, Depends, HTTPException

from forven.api_security import require_operator_access

router = APIRouter(tags=["breadth"], dependencies=[Depends(require_operator_access)])


@router.get("/api/strategies/{strategy_id}/breadth")
def get_strategy_breadth(strategy_id: str) -> dict:
    from forven.breadth import get_breadth

    try:
        return get_breadth(strategy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"strategy not found: {strategy_id}") from None


@router.post("/api/strategies/{strategy_id}/breadth")
def post_strategy_breadth(strategy_id: str, refresh: bool = False) -> dict:
    from forven.breadth import start_breadth

    try:
        return start_breadth(strategy_id, refresh=refresh)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"strategy not found: {strategy_id}") from None
