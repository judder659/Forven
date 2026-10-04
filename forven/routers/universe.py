"""Universe books: pre-registered rules across many coins (forven.universe).

Part of the portfolio layer: every route 404s while that layer's master switch is
off. Paper only.
"""

from fastapi import APIRouter, Depends, HTTPException

from forven.api_security import require_operator_access
from forven.control_plane.models import ConfirmBody

router = APIRouter(tags=["universe"], dependencies=[Depends(require_operator_access)])


def _require_portfolio_layer() -> None:
    from forven.portfolio_allocator import portfolio_layer_enabled

    if not portfolio_layer_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


def _require_book(name: str) -> None:
    from forven.universe.strategies import BOOKS

    if name not in BOOKS:
        raise HTTPException(status_code=404, detail=f"unknown universe book: {name}")


@router.get("/api/universe/books")
def get_universe_books() -> dict:
    _require_portfolio_layer()
    from forven.universe.book import book_summary, universe_books_enabled
    from forven.universe.strategies import BOOKS

    return {"ok": True, "enabled": universe_books_enabled(), "books": [book_summary(name) for name in BOOKS]}


# Sync def: the first report of the day loads ~6 years of 1h bars for 15 coins
# (seconds), so it runs in the threadpool, never on the request loop.
@router.get("/api/universe/books/{name}/research")
def get_universe_book_research(name: str, refresh: bool = False) -> dict:
    _require_portfolio_layer()
    _require_book(name)
    from forven.universe.research import research_report

    return {"ok": True, "report": research_report(name, refresh=refresh)}


@router.post("/api/universe/tick")
def post_universe_tick() -> dict:
    _require_portfolio_layer()
    from forven.universe.book import run_universe_tick

    return {"ok": True, "report": run_universe_tick()}


@router.post("/api/universe/books/{name}/reset")
def post_universe_book_reset(name: str, body: ConfirmBody) -> dict:
    _require_portfolio_layer()
    _require_book(name)
    if not bool(body.confirm):
        raise HTTPException(status_code=400, detail="confirmation required to reset a universe book")
    from forven.universe.book import reset_book

    return {"ok": reset_book(name), "book": name}
